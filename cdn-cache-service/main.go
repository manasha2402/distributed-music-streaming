package main

import (
	"encoding/json"
	"fmt"
	"log"
	"net/http"
	"os"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"time"
)

// ─── Constants ────────────────────────────────────────────────────────────────

const (
	DefaultNumSongs   = 100  // songs pre-populated at startup
	DefaultNumChunks  = 300  // chunks per song (~30 seconds of audio)
	HighChunkBytes    = 4000 // mirrors streaming service
	PopularSongPrefix = "song_" // song IDs 0–9 are "popular" (always cached)
)

// ─── Models ───────────────────────────────────────────────────────────────────

type cacheKey struct {
	SongID     string
	ChunkIndex int
}

// adminConfig allows test scenarios to inject failure/latency.
type adminConfig struct {
	Failing  bool
	DelayMs  int
	mu       sync.RWMutex
}

func (a *adminConfig) get() (failing bool, delayMs int) {
	a.mu.RLock()
	defer a.mu.RUnlock()
	return a.Failing, a.DelayMs
}

func (a *adminConfig) set(failing bool, delayMs int) {
	a.mu.Lock()
	defer a.mu.Unlock()
	a.Failing = failing
	a.DelayMs = delayMs
}

type cdnService struct {
	cache    sync.Map // map[cacheKey][]byte
	hits     atomic.Int64
	misses   atomic.Int64
	requests atomic.Int64
	admin    adminConfig
}

// ─── Request / Response ───────────────────────────────────────────────────────

type metricsResp struct {
	TotalRequests int64   `json:"total_requests"`
	CacheHits     int64   `json:"cache_hits"`
	CacheMisses   int64   `json:"cache_misses"`
	HitRate       float64 `json:"hit_rate"`
	CacheSize     int64   `json:"cache_size"`
	Failing       bool    `json:"failing"`
	DelayMs       int     `json:"delay_ms"`
}

type adminReq struct {
	Fail    bool `json:"fail"`
	DelayMs int  `json:"delay_ms"`
}

// ─── Init ─────────────────────────────────────────────────────────────────────

// populate fills the cache with numSongs × numChunks synthetic audio chunks.
func (c *cdnService) populate(numSongs, numChunks int) {
	for song := 0; song < numSongs; song++ {
		for chunk := 0; chunk < numChunks; chunk++ {
			key := cacheKey{SongID: fmt.Sprintf("%s%d", PopularSongPrefix, song), ChunkIndex: chunk}
			data := make([]byte, HighChunkBytes)
			for i := range data {
				data[i] = byte((song*chunk + i) & 0xFF)
			}
			c.cache.Store(key, data)
		}
	}
	log.Printf("cache pre-populated: %d songs × %d chunks", numSongs, numChunks)
}

// ─── Routes ───────────────────────────────────────────────────────────────────

func (c *cdnService) routes() *http.ServeMux {
	mux := http.NewServeMux()
	mux.HandleFunc("/health", c.health)
	mux.HandleFunc("/metrics", c.metrics)
	mux.HandleFunc("/cache/", c.handleCache)
	mux.HandleFunc("/admin/config", c.handleAdminConfig)
	return mux
}

// ─── Handlers ─────────────────────────────────────────────────────────────────

func (c *cdnService) health(w http.ResponseWriter, r *http.Request) {
	failing, _ := c.admin.get()
	if failing {
		// Still respond to ALB health checks; actual chunk requests will fail.
		w.WriteHeader(http.StatusOK)
		fmt.Fprint(w, "OK (degraded)")
		return
	}
	w.WriteHeader(http.StatusOK)
	fmt.Fprint(w, "OK")
}

func (c *cdnService) metrics(w http.ResponseWriter, r *http.Request) {
	hits := c.hits.Load()
	total := c.requests.Load()
	hitRate := 0.0
	if total > 0 {
		hitRate = float64(hits) / float64(total) * 100
	}
	cacheSize := int64(0)
	c.cache.Range(func(_, _ interface{}) bool { cacheSize++; return true })

	failing, delayMs := c.admin.get()
	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(metricsResp{
		TotalRequests: total,
		CacheHits:     hits,
		CacheMisses:   c.misses.Load(),
		HitRate:       hitRate,
		CacheSize:     cacheSize,
		Failing:       failing,
		DelayMs:       delayMs,
	})
}

// GET  /cache/{songId}/{chunkIndex}  — retrieve chunk
// PUT  /cache/{songId}/{chunkIndex}  — store chunk
func (c *cdnService) handleCache(w http.ResponseWriter, r *http.Request) {
	songID, chunkIndex, ok := parseCachePath(r.URL.Path)
	if !ok {
		http.Error(w, "invalid path — expect /cache/{songId}/{chunkIndex}", http.StatusBadRequest)
		return
	}

	switch r.Method {
	case http.MethodGet:
		c.getChunk(w, songID, chunkIndex)
	case http.MethodPut:
		c.putChunk(w, r, songID, chunkIndex)
	default:
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
	}
}

func (c *cdnService) getChunk(w http.ResponseWriter, songID string, chunkIndex int) {
	c.requests.Add(1)

	failing, delayMs := c.admin.get()
	if failing {
		if delayMs > 0 {
			time.Sleep(time.Duration(delayMs) * time.Millisecond)
		}
		http.Error(w, "CDN service unavailable", http.StatusServiceUnavailable)
		return
	}
	if delayMs > 0 {
		time.Sleep(time.Duration(delayMs) * time.Millisecond)
	}

	key := cacheKey{SongID: songID, ChunkIndex: chunkIndex}
	val, ok := c.cache.Load(key)
	if !ok {
		c.misses.Add(1)
		http.Error(w, "chunk not found", http.StatusNotFound)
		return
	}
	c.hits.Add(1)
	w.Header().Set("Content-Type", "application/octet-stream")
	w.Write(val.([]byte))
}

func (c *cdnService) putChunk(w http.ResponseWriter, r *http.Request, songID string, chunkIndex int) {
	key := cacheKey{SongID: songID, ChunkIndex: chunkIndex}
	data := make([]byte, HighChunkBytes)
	c.cache.Store(key, data)
	w.WriteHeader(http.StatusNoContent)
}

// POST /admin/config  — inject failures or latency for circuit breaker tests
func (c *cdnService) handleAdminConfig(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	var req adminReq
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		http.Error(w, "invalid JSON", http.StatusBadRequest)
		return
	}
	c.admin.set(req.Fail, req.DelayMs)
	log.Printf("admin config updated: fail=%v delay=%dms", req.Fail, req.DelayMs)
	w.WriteHeader(http.StatusNoContent)
}

// ─── Helpers ──────────────────────────────────────────────────────────────────

// parseCachePath parses /cache/{songId}/{chunkIndex}
func parseCachePath(path string) (songID string, chunkIndex int, ok bool) {
	trimmed := strings.TrimPrefix(path, "/cache/")
	parts := strings.SplitN(trimmed, "/", 2)
	if len(parts) != 2 || parts[0] == "" || parts[1] == "" {
		return "", 0, false
	}
	idx, err := strconv.Atoi(parts[1])
	if err != nil {
		return "", 0, false
	}
	return parts[0], idx, true
}

// ─── Main ─────────────────────────────────────────────────────────────────────

func main() {
	port := os.Getenv("PORT")
	if port == "" {
		port = "8081"
	}

	c := &cdnService{}
	c.populate(DefaultNumSongs, DefaultNumChunks)

	log.Printf("CDN cache service on :%s", port)
	log.Fatal(http.ListenAndServe(":"+port, c.routes()))
}
