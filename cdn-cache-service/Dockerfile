FROM golang:1.21-alpine AS builder
WORKDIR /app
COPY go.mod .
COPY main.go .
RUN go build -o cdn-cache-service .

FROM alpine:3.18
WORKDIR /app
COPY --from=builder /app/cdn-cache-service .
EXPOSE 8081
CMD ["./cdn-cache-service"]
