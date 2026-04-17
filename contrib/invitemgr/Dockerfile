FROM golang:1.21-alpine AS builder
WORKDIR /app
RUN apk add --no-cache gcc musl-dev
COPY . .
RUN go mod init invite-widget && \
    go get github.com/mattn/go-sqlite3 && \
    CGO_ENABLED=1 GOOS=linux go build -a -o server main.go

FROM alpine:latest
WORKDIR /app
COPY --from=builder /app/server .
COPY *.html .
RUN mkdir data libs
CMD ["./server"]
