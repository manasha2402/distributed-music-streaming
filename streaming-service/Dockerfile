FROM golang:1.21-alpine AS builder
WORKDIR /app
COPY go.mod .
COPY main.go .
RUN go build -o streaming-service .

FROM alpine:3.18
WORKDIR /app
COPY --from=builder /app/streaming-service .
EXPOSE 8080
CMD ["./streaming-service"]
