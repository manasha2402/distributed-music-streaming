package main

import (
	"context"
	"crypto/rand"
	"encoding/json"
	"fmt"
	"log"
	"net/http"
	"os"
	"strings"
	"sync"
	"sync/atomic"
	"time"
)

// ─── Constants ────────────────────────────────────────────────────────────────

const (
	HighQualityBitrate = 320 // kbps
	LowQualityBitrate  = 128 // kbps
	ChunkDurationMs    = 100 // ms of audio per chunk

	// Chunk sizes: bitrate(kbps)*1000/8 bits * duration(s)
	HighChunkBytes = HighQualityBitrate * 1000 / 8 * ChunkDurationMs / 1000 // 4000 bytes
	LowChunkBytes  = LowQualityBitrate * 1000 / 8 * ChunkDurationMs / 1000  // 1600 bytes

	HighChunkWork = 500_000 // CPU iterations per high-quality chunk
	LowChunkWork  = 50_000  // CPU iterations per low-quality chunk

	LatencyThresholdMs  = 300 // switch quality above this (adaptive mode)
	RebufferThresholdMs = 500 // count as rebuffering event above this
	LowQualityCooldown  = 15  // min chunks at low quality before recovery is allowed
	CDNTimeoutMs        = 500 // fail fast: abort CDN call after this
	CBFailureLimit      = 5   // trip circuit breaker after this many failures
	CBResetDuration     = 30 * time.Second
)

// ─── Models ───────────────────────────────────────────────────────────────────

// Stream represents one active audio stream (one goroutine-safe state blob).
type Stream struct {
	ID              string    `json:"id"`
	SongID          string    `json:"song_id"`
	Quality         string    `json:"quality"` // "high" or "low"
	CurrentChunk    int       `json:"current_chunk"`
	StartedAt       time.Time `json:"started_at"`
	RebufferCount   int       `json:"rebuffer_count"`
	QualitySwitches int       `json:"quality_switches"`
	Adaptive        bool      `json:"adaptive"`
	lowCooldown     int       // chunks remaining before recovery to high is allowed
	mu              sync.Mutex
}

// circuitBreaker protects downstream CDN calls.
type circuitBreaker struct {
	failures    int64 // accessed via atomic
	tripped     bool
	lastTripped time.Time
	mu          sync.Mutex
}

func (cb *circuitBreaker) isOpen() bool {
	cb.mu.Lock()
	defer cb.mu.Unlock()
	if cb.tripped && time.Since(cb.lastTripped) > CBResetDuration {
		cb.tripped = false
		atomic.StoreInt64(&cb.failures, 0)
		log.Println("circuit breaker: reset → closed")
	}
	return cb.tripped
}

func (cb *circuitBreaker) recordFailure() {
	n := atomic.AddInt64(&cb.failures, 1)
	if n >= CBFailureLimit {
		cb.mu.Lock()
		if !cb.tripped {
			cb.tripped = true
			cb.lastTripped = time.Now()
			log.Printf("circuit breaker: OPEN after %d failures", n)
		}
		cb.mu.Unlock()
	}
}

func (cb *circuitBreaker) recordSuccess() {
	atomic.StoreInt64(&cb.failures, 0)
}

// ─── Service ──────────────────────────────────────────────────────────────────

type svc struct {
	streamMap       sync.Map // map[string]*Stream
	totalStreams     atomic.Int64
	totalRebuffers  atomic.Int64
	totalQSwitches  atomic.Int64
	adaptiveStreams  atomic.Int64
	fixedStreams     atomic.Int64
	adaptiveRebuf   atomic.Int64
	fixedRebuf      atomic.Int64
	cb              circuitBreaker
	cdnURL          string
	httpClient      *http.Client
}

// ─── Request / Response types ─────────────────────────────────────────────────

type startReq struct {
	SongID   string `json:"song_id"`
	Adaptive bool   `json:"adaptive"`
}

type startResp struct {
	StreamID string `json:"stream_id"`
	Quality  string `json:"quality"`
}

type chunkResp struct {
	StreamID        string  `json:"stream_id"`
	ChunkIndex      int     `json:"chunk_index"`
	Quality         string  `json:"quality"`
	SizeBytes       int     `json:"size_bytes"`
	LatencyMs       float64 `json:"latency_ms"`
	Rebuffered      bool    `json:"rebuffered"`
	QualitySwitched bool    `json:"quality_switched"`
	FromCDN         bool    `json:"from_cdn"`
}

type metricsResp struct {
	ActiveStreams    int64 `json:"active_streams"`
	TotalStreams     int64 `json:"total_streams"`
	TotalRebuffers  int64 `json:"total_rebuffers"`
	TotalQSwitches  int64 `json:"total_quality_switches"`
	AdaptiveStreams  int64 `json:"adaptive_streams"`
	FixedStreams     int64 `json:"fixed_streams"`
	AdaptiveRebuf   int64 `json:"adaptive_rebuffers"`
	FixedRebuf      int64 `json:"fixed_rebuffers"`
	CDNCircuitOpen  bool  `json:"cdn_circuit_open"`
}

// ─── Helpers ──────────────────────────────────────────────────────────────────

func newID() string {
	b := make([]byte, 16)
	if _, err := rand.Read(b); err != nil {
		return fmt.Sprintf("%d", time.Now().UnixNano())
	}
	return fmt.Sprintf("%x%x%x%x%x", b[0:4], b[4:6], b[6:8], b[8:10], b[10:16])
}

func generateChunk(quality string) []byte {
	work, size := HighChunkWork, HighChunkBytes
	if quality == "low" {
		work, size = LowChunkWork, LowChunkBytes
	}
	data := make([]byte, size)
	n := 0
	for i := 0; i < work; i++ {
		n = (n*6364136223846793005 + 1442695040888963407) & 0xFF
	}
	data[0] = byte(n)
	return data
}

// ─── Route registration ───────────────────────────────────────────────────────

func (s *svc) routes() *http.ServeMux {
	mux := http.NewServeMux()
	mux.HandleFunc("/health", s.health)
	mux.HandleFunc("/metrics", s.metrics)
	mux.HandleFunc("/debug/reset", s.handleReset)
	mux.HandleFunc("/streams", s.handleCreateStream)
	mux.HandleFunc("/streams/", s.handleStream)
	return mux
}

// ─── Handlers ─────────────────────────────────────────────────────────────────

func (s *svc) health(w http.ResponseWriter, r *http.Request) {
	w.WriteHeader(http.StatusOK)
	fmt.Fprint(w, "OK")
}

func (s *svc) metrics(w http.ResponseWriter, r *http.Request) {
	active := int64(0)
	s.streamMap.Range(func(_, _ interface{}) bool { active++; return true })
	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(metricsResp{
		ActiveStreams:   active,
		TotalStreams:    s.totalStreams.Load(),
		TotalRebuffers: s.totalRebuffers.Load(),
		TotalQSwitches: s.totalQSwitches.Load(),
		AdaptiveStreams: s.adaptiveStreams.Load(),
		FixedStreams:    s.fixedStreams.Load(),
		AdaptiveRebuf:  s.adaptiveRebuf.Load(),
		FixedRebuf:     s.fixedRebuf.Load(),
		CDNCircuitOpen: s.cb.isOpen(),
	})
}

func (s *svc) handleReset(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	s.totalStreams.Store(0)
	s.totalRebuffers.Store(0)
	s.totalQSwitches.Store(0)
	s.adaptiveStreams.Store(0)
	s.fixedStreams.Store(0)
	s.adaptiveRebuf.Store(0)
	s.fixedRebuf.Store(0)
	s.streamMap.Range(func(k, _ interface{}) bool { s.streamMap.Delete(k); return true })
	log.Println("metrics reset via /admin/reset")
	w.WriteHeader(http.StatusNoContent)
}

// POST /streams  — create a new stream
func (s *svc) handleCreateStream(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	var req startReq
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		http.Error(w, "invalid JSON", http.StatusBadRequest)
		return
	}
	if req.SongID == "" {
		req.SongID = fmt.Sprintf("song_%d", time.Now().UnixNano()%100)
	}
	stream := &Stream{
		ID:        newID(),
		SongID:    req.SongID,
		Quality:   "high",
		StartedAt: time.Now(),
		Adaptive:  req.Adaptive,
	}
	s.streamMap.Store(stream.ID, stream)
	s.totalStreams.Add(1)
	if stream.Adaptive {
		s.adaptiveStreams.Add(1)
	} else {
		s.fixedStreams.Add(1)
	}

	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(http.StatusCreated)
	json.NewEncoder(w).Encode(startResp{StreamID: stream.ID, Quality: stream.Quality})
}

// GET /streams/{id}/chunk  |  GET /streams/{id}  |  DELETE /streams/{id}
func (s *svc) handleStream(w http.ResponseWriter, r *http.Request) {
	rest := strings.TrimPrefix(r.URL.Path, "/streams/")
	parts := strings.SplitN(rest, "/", 2)
	if len(parts) == 0 || parts[0] == "" {
		http.Error(w, "missing stream id", http.StatusBadRequest)
		return
	}
	id := parts[0]

	switch {
	case len(parts) == 2 && parts[1] == "chunk" && r.Method == http.MethodGet:
		s.getChunk(w, r, id)
	case len(parts) == 1 && r.Method == http.MethodGet:
		s.getStreamStats(w, r, id)
	case len(parts) == 1 && r.Method == http.MethodDelete:
		s.deleteStream(w, r, id)
	default:
		http.Error(w, "not found", http.StatusNotFound)
	}
}

// GET /streams/{id}/chunk — core streaming endpoint
func (s *svc) getChunk(w http.ResponseWriter, r *http.Request, id string) {
	val, ok := s.streamMap.Load(id)
	if !ok {
		http.Error(w, "stream not found", http.StatusNotFound)
		return
	}
	stream := val.(*Stream)

	start := time.Now()

	// Fast path: try CDN (circuit breaker + 500 ms fail-fast)
	chunkData, fromCDN := s.fetchFromCDN(stream.SongID, stream.CurrentChunk)
	if !fromCDN {
		// Slow path: generate locally (CPU-bound, simulates transcoding)
		stream.mu.Lock()
		quality := stream.Quality
		stream.mu.Unlock()
		chunkData = generateChunk(quality)
	}

	latencyMs := float64(time.Since(start).Microseconds()) / 1000.0

	stream.mu.Lock()
	defer stream.mu.Unlock()

	rebuffered := latencyMs > float64(RebufferThresholdMs)
	qualitySwitched := false

	if rebuffered {
		stream.RebufferCount++
		s.totalRebuffers.Add(1)
		if stream.Adaptive {
			s.adaptiveRebuf.Add(1)
		} else {
			s.fixedRebuf.Add(1)
		}
	}
	if stream.Adaptive {
		if latencyMs > float64(LatencyThresholdMs) && stream.Quality == "high" {
			// Slow chunk — degrade and start cooldown
			stream.Quality = "low"
			stream.lowCooldown = LowQualityCooldown
			stream.QualitySwitches++
			s.totalQSwitches.Add(1)
			qualitySwitched = true
		} else if stream.Quality == "low" {
			if stream.lowCooldown > 0 {
				stream.lowCooldown--
			} else if latencyMs < float64(LatencyThresholdMs/2) {
				// Cooldown expired and chunk was fast — recover to high
				stream.Quality = "high"
				stream.QualitySwitches++
				s.totalQSwitches.Add(1)
				qualitySwitched = true
			}
		}
	}
	stream.CurrentChunk++

	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(chunkResp{
		StreamID:        id,
		ChunkIndex:      stream.CurrentChunk - 1,
		Quality:         stream.Quality,
		SizeBytes:       len(chunkData),
		LatencyMs:       latencyMs,
		Rebuffered:      rebuffered,
		QualitySwitched: qualitySwitched,
		FromCDN:         fromCDN,
	})
}

func (s *svc) fetchFromCDN(songID string, chunkIndex int) ([]byte, bool) {
	if s.cb.isOpen() {
		return nil, false // circuit open: skip CDN entirely
	}
	ctx, cancel := context.WithTimeout(context.Background(), CDNTimeoutMs*time.Millisecond)
	defer cancel()

	url := fmt.Sprintf("%s/cache/%s/%d", s.cdnURL, songID, chunkIndex)
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	if err != nil {
		return nil, false
	}
	resp, err := s.httpClient.Do(req)
	if err != nil {
		s.cb.recordFailure()
		return nil, false
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		s.cb.recordFailure()
		return nil, false
	}
	s.cb.recordSuccess()
	return make([]byte, HighChunkBytes), true // simulated payload
}

func (s *svc) getStreamStats(w http.ResponseWriter, r *http.Request, id string) {
	val, ok := s.streamMap.Load(id)
	if !ok {
		http.Error(w, "stream not found", http.StatusNotFound)
		return
	}
	stream := val.(*Stream)
	stream.mu.Lock()
	defer stream.mu.Unlock()
	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(stream)
}

func (s *svc) deleteStream(w http.ResponseWriter, r *http.Request, id string) {
	if _, ok := s.streamMap.Load(id); !ok {
		http.Error(w, "stream not found", http.StatusNotFound)
		return
	}
	s.streamMap.Delete(id)
	w.WriteHeader(http.StatusNoContent)
}

// ─── Main ─────────────────────────────────────────────────────────────────────

func main() {
	cdnURL := os.Getenv("CDN_URL")
	if cdnURL == "" {
		cdnURL = "http://localhost:8081"
	}
	port := os.Getenv("PORT")
	if port == "" {
		port = "8080"
	}

	s := &svc{
		cdnURL:     cdnURL,
		httpClient: &http.Client{Timeout: 2 * time.Second},
	}

	log.Printf("streaming service on :%s  (CDN: %s)", port, cdnURL)
	log.Fatal(http.ListenAndServe(":"+port, s.routes()))
}
