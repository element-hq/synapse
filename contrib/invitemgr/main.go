package main

import (
	"bytes"
	"database/sql"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"log"
	"net/http"
	"net/url"
	"os"
	"strings"
	"time"
	"sync"
	"net"
	"io"
	"context"
	"os/signal"
	"syscall"
	"strconv"

	_ "github.com/mattn/go-sqlite3"
)

// ipLimiter tracks the last time an IP visited to prevent brute-force
type ipLimiter struct {
	lastSeen time.Time
}

var (
	visitors = make(map[string]*ipLimiter)
	mu       sync.Mutex // Protects the visitors map from concurrent access
)

// Global HTTP client with defined timeouts
var httpClient = &http.Client{
    Timeout: 5 * time.Second,
}

var (
	db                 	*sql.DB
	synapseURL         	= os.Getenv("SYNAPSE_URL")
	synapseAdminUser   	= os.Getenv("SYNAPSE_ADMIN_USER")
	synapseAdminPass   	= os.Getenv("SYNAPSE_ADMIN_PASSWORD")
	webclientUrl		= os.Getenv("WEBCLIENT_URL")
	port				= os.Getenv("PORT")
	proxyAddr          	= os.Getenv("PROXY_ADDR")
	noTLS              	= os.Getenv("NO_TLS")
	widgetAPIMode	   	= os.Getenv("WIDGET_API")
	widgetAPIURL	   	= os.Getenv("WIDGET_API_URL")
	invitesLimitUsr	   	= os.Getenv("INVITES_PER_USER")
	invitesLimit	   	= 5
	srvPort 			= ":8080"
	
	// Thread-safe storage for the Synapse admin access token
	adminToken         string
	tokenMu            sync.RWMutex
)

func main() {
	if synapseURL == "" || synapseAdminUser == "" || synapseAdminPass == "" || webclientUrl == "" {
        log.Fatal("SYNAPSE_URL, SYNAPSE_ADMIN_USER, SYNAPSE_ADMIN_PASSWORD and WEBCLIENT_URL are required")
    }
	
	// Determine the Widget API URL based on mode
	var finalWidgetURL string
	if widgetAPIMode == "external" {
		if widgetAPIURL != "" {
			finalWidgetURL = widgetAPIURL
		} else {
			finalWidgetURL = "https://esm.sh/matrix-widget-api@1.17.0"
		}
	} else {
		widgetAPIMode = "internal"
		finalWidgetURL = "/libs/widget-api.min.js"
	}
	log.Printf("Widget API mode: %s, URL: %s", widgetAPIMode, finalWidgetURL)
	
	// Configure server port
	if port != "" {
		_, err := strconv.Atoi(port)
        if err != nil {
            log.Printf("WARNING: PORT is not a number (%s). Using default: %s", port, srvPort)
		} else {
			log.Printf("Setting server port to %s", port)
			srvPort = ":"+port
		}
		
	}
	
	// Configure per-user invite limits
	if invitesLimitUsr == "" {
		log.Printf("WARNING: INVITES_PER_USER not defined. Using default: %d", invitesLimit)
	} else {
        val, err := strconv.Atoi(invitesLimitUsr)
        if err != nil {
            log.Printf("WARNING: INVITES_PER_USER is not a number (%s). Using default: %d", invitesLimitUsr, invitesLimit)
        } else if val < 0 {
            log.Printf("WARNING: INVITES_PER_USER cannot be negative (%d). Using default: %s", val, invitesLimit)
        } else {
            invitesLimit = val
			log.Printf("Maximum invites per user set to %d.", invitesLimit)
            // if val == 0, its legal: users invites creation disabled
        }
    }
	
	// Security check: ensure HTTPS is used unless explicitly bypassed
	if noTLS == "I_KNOW_WHAT_I_DO" {
		noTLS = "1"
		log.Println("WARNING!!! NO_TLS VALUE IS SET! SERVER WILL IGNORE MISSING ENCRIPTION!!! YOU MUST KNOW WHAT YOU DO!!!")
	} else {
		noTLS = "0"
	}
	
	if !strings.HasPrefix(synapseURL, "https://") && noTLS != "1" {
		log.Fatal("SYNAPSE_URL must use HTTPS")
	}
	
	if !strings.HasPrefix(webclientUrl, "https://") && noTLS != "1" {
		log.Fatal("SYNAPSE_URL must use HTTPS")
	}
	
	if proxyAddr == "" {
		log.Println("WARNING: Directive PROXY_ADDR is not set. Do you know what you do?! Please set this to actual if you use the proxy server (Caddy, Nginx, etc) before")
	}

	var err error
	// Initialize SQLite (stored in a persistent docker volume)
	db, err = sql.Open("sqlite3", "./data/invites.db?_busy_timeout=5000")
	if err != nil {
		log.Fatal(err)
	}
	
	// Enable Write-Ahead Logging (WAL) for better concurrency
	if _, err := db.Exec("PRAGMA journal_mode=WAL;"); err != nil {
		log.Printf("Failed to enable WAL: %v", err)
	}

	db.SetMaxOpenConns(5)
	db.SetMaxIdleConns(5)
	db.SetConnMaxLifetime(time.Hour)

	_, err = db.Exec(`CREATE TABLE IF NOT EXISTS tokens (
		id INTEGER PRIMARY KEY AUTOINCREMENT,
		user_id TEXT,
		token TEXT UNIQUE,
		created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
		deleted INTEGER NOT NULL DEFAULT 0
	)`)
	if err != nil {
		log.Fatal(err)
	}

	// Add indexes
	_, _ = db.Exec("CREATE INDEX IF NOT EXISTS idx_active_tokens ON tokens(user_id, deleted)")
	
	// Run the admin token refresher in the background
    go startTokenRefresher()
	
	// Static libraries handler
	http.Handle("/libs/", http.StripPrefix("/libs/", http.FileServer(http.Dir("libs"))))
	
	// Main UI and Widget handler
	http.HandleFunc("/", func(w http.ResponseWriter, r *http.Request) {
		
		// Deny for scaning bots, if the try to serach something like /wp-admin, /.env, etc...
		if r.URL.Path != "/" {
            http.NotFound(w, r)
            return
        }
		
		if !strings.Contains(r.URL.RawQuery, "widgetId") {
			http.ServeFile(w, r, "index.html")
			return
		}
		
		private, err := readPage("private.html")
		if err != nil {
			http.Error(w, "Internal Server Error", http.StatusInternalServerError)
			return
		}

		replaced := strings.Replace(string(private), "[[WIDGET_API_URL]]", finalWidgetURL, -1)
		replaced = strings.Replace(replaced, "[[INVITES_LIMIT]]", strconv.Itoa(invitesLimit), -1)
		
		w.Header().Set("Content-Type", "text/html; charset=utf-8")
		fmt.Fprint(w, replaced)
		


	})
	
	// Guest invitation landing page
	http.HandleFunc("/invites/", func(w http.ResponseWriter, r *http.Request) {

		if r.URL.Path == "/invites/" {
			http.NotFound(w, r)
			return
		}

		guest, err := readPage("guest.html")
		if err != nil {
			http.Error(w, "Internal Server Error", http.StatusInternalServerError)
			return
		}
		
		replaced := strings.Replace(string(guest), "[[WEBCLIENT_URL]]", webclientUrl, -1)
		
		w.Header().Set("Content-Type", "text/html; charset=utf-8")
		fmt.Fprint(w, replaced)
		
	})
	
	// API endpoints
	http.HandleFunc("/api/tokens", handleTokens)
	http.HandleFunc("/api/tokens/", handleDeleteToken)
    http.HandleFunc("/api/public/check/", handleCheckToken)
	
	// Clean up visitors map once an hour
	go cleanupVisitors() 
	
	srv := &http.Server{
		Addr:              srvPort,
		Handler:           nil,
		ReadTimeout:       5 * time.Second,
		WriteTimeout:      10 * time.Second,
		IdleTimeout:       60 * time.Second,
		ReadHeaderTimeout: 2 * time.Second,
	}
	
	signalChan := make(chan os.Signal, 1)
	signal.Notify(signalChan, syscall.SIGINT, syscall.SIGTERM)

	go func() {
		sig := <-signalChan
		log.Println("Shutting down...", sig)

		ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
    
		if err := srv.Shutdown(ctx); err != nil {
			log.Println("Shutdown error:", err)
		}
		
		if db != nil {
			log.Println("Closing database...")
			db.Close() 
		}
		
	}()

	log.Printf("Server started on %s", srvPort)
	if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
		log.Fatalf("Listen error: %s\n", err)
	}

}

// readPage reads a file from disk and returns its content
func readPage(page string) ([]byte, error) {	
	content, err := os.ReadFile(page)
		if err != nil {
			log.Printf("Error reading %s: %v", page, err)
			return nil, err
		}
	return content, nil
}

// performLogin authenticates as a Synapse Admin to retrieve an access token
func performLogin() error {
	loginData := map[string]interface{}{
		"type": "m.login.password",
		"identifier": map[string]string{
			"type": "m.id.user",
			"user": synapseAdminUser,
		},
		"password": synapseAdminPass,
	}

	body, _ := json.Marshal(loginData)
	resp, err := httpClient.Post(
		synapseURL+"/_matrix/client/v3/login", 
		"application/json", 
		bytes.NewBuffer(body))
	if err != nil {
		return err
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("login failed with status: %s", resp.Status)
	}

	var result struct {
		AccessToken string `json:"access_token"`
	}
	decoder := json.NewDecoder(io.LimitReader(resp.Body, 1<<20)) // 1MB
	if err := decoder.Decode(&result); err != nil {
		return err
	}

	tokenMu.Lock()
	adminToken = result.AccessToken
	tokenMu.Unlock()
	
	if err := saveTokenToFile(result.AccessToken); err != nil {
		log.Printf("Unable to save admin token to file. Error: %v", err)
	}

	log.Println("Successfully refreshed Synapse Admin Token")
	return nil
}

// startTokenRefresher handles periodic token validation and renewal
func startTokenRefresher() {
	loginErrors := 0
	for {
		currentToken := getAdminToken()
		if currentToken == "" {
			currentToken = getSavedFileToken()
		}		
		
		if !isTokenValid(currentToken) {
			log.Println("Admin token is invalid or expired. Performing login...")
			time.Sleep(5 * time.Second)
			if err := performLogin(); err != nil {
				if loginErrors > 9 {
					log.Fatalf("Login failed 10 times: %v. Check admin login/password at settings and restart the service", err)
				}
				log.Printf("Login failed: %v. Will retry for 1 minute", err)
				loginErrors++
				time.Sleep(1 * time.Minute)
				continue
			}
		}

		time.Sleep(10 * time.Minute)
	}
}

// saveTokenToFile persists the admin token to the data directory
func saveTokenToFile(token string) error {
	tmp := "./data/admin_token.tmp"
	final := "./data/admin_token"

	if err := os.WriteFile(tmp, []byte(token), 0600); err != nil {
		log.Printf("Failed to write temp token: %v", err)
		return err
	}

	if err := os.Rename(tmp, final); err != nil {
		log.Printf("Failed to rename token file: %v", err)
		return err
	}
	return nil
}

// getSavedFileToken attempts to load a previously saved token from disk
func getSavedFileToken() string {
	
	data, err := os.ReadFile("./data/admin_token")
	if err != nil {
		log.Printf("Unable to load admin token from file. Error: %v", err)
		return ""
	}

	savedToken := strings.TrimSpace(string(data))
	tokenMu.Lock()
	adminToken = savedToken
	tokenMu.Unlock()
	return savedToken
	
}

// waitForAdminToken polls for an available admin token with a timeout
func waitForAdminToken() (string, error) {
	timeout := time.After(10 * time.Second)
    for {
		select {
		case <-timeout:
			return "", fmt.Errorf("timeout waiting for admin token")
		default:
			token := getAdminToken()
			if token != "" {
				return token, nil
			}
			time.Sleep(100 * time.Millisecond)
		}
	}
}

// isTokenValid checks the validity of a Synapse token via the /whoami endpoint
func isTokenValid(token string) bool {
	if token == "" {
		return false
	}
	
	req, err := http.NewRequest(http.MethodGet, synapseURL+"/_matrix/client/v3/account/whoami", nil)
	if err != nil {
		return false
	}
	req.Header.Set("Authorization", "Bearer "+token)
	
	resp, err := httpClient.Do(req)
	if err != nil {
		return false
	}
	defer resp.Body.Close()

	return resp.StatusCode == http.StatusOK
}

// getAdminToken returns the current global admin token in a thread-safe way
func getAdminToken() string {
	tokenMu.RLock()
	defer tokenMu.RUnlock()
	return adminToken
}


// OIDCToken holds OpenID Connect data from the Matrix Widget API
type OIDCToken struct {
	AccessToken      string `json:"access_token"`
	ExpiresIn        int    `json:"expires_in"`
	MatrixServerName string `json:"matrix_server_name"`
	TokenType        string `json:"token_type"`
}

// authenticate verifies the user's OIDC token with Synapse and returns their user_id
func authenticate(r *http.Request) (string, error) {
	authHeader := r.Header.Get("Authorization")
	if !strings.HasPrefix(authHeader, "Bearer ") {
		return "", fmt.Errorf("missing bearer token")
	}

	b64Token := strings.TrimPrefix(authHeader, "Bearer ")
	tokenJSON, err := base64.StdEncoding.DecodeString(b64Token)
	if err != nil {
		return "", err
	}

	var oidc OIDCToken
	if err := json.Unmarshal(tokenJSON, &oidc); err != nil {
		return "", err
	}
	
	safeToken := url.QueryEscape(oidc.AccessToken)
	url := fmt.Sprintf("%s/_matrix/federation/v1/openid/userinfo?access_token=%s", synapseURL, safeToken)
	resp, err := httpClient.Get(url)
	if err != nil {
		return "", err
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		return "", fmt.Errorf("invalid oidc token")
	}

	var userInfo struct {
		Sub string `json:"sub"`
	}
	decoder := json.NewDecoder(io.LimitReader(resp.Body, 1<<20)) // 1MB
	if err := decoder.Decode(&userInfo); err != nil {
		return "", err
	}

	return userInfo.Sub, nil
}

// getRealIP extracts the client IP, supporting X-Forwarded-For if behind a trusted proxy
func getRealIP(r *http.Request) string {
    host, _, err := net.SplitHostPort(r.RemoteAddr)
    if err != nil {
        host = r.RemoteAddr
    }

    if proxyAddr == "" {
        return host
    }

    parsedHost := net.ParseIP(host)
    _, trustedNet, err := net.ParseCIDR(proxyAddr)
    
    if host == proxyAddr || (err == nil && trustedNet.Contains(parsedHost)) {
        xff := r.Header.Get("X-Forwarded-For")
        if xff != "" {
            parts := strings.Split(xff, ",")
            return strings.TrimSpace(parts[len(parts)-1])
        }
    }

    return host
}

// handleCheckToken allows guests to check if a registration token is still valid
func handleCheckToken(w http.ResponseWriter, r *http.Request) {
	
	ip := getRealIP(r)
	if isRateLimited(ip) {
		log.Printf("Rate limit hit for IP: %s", ip)
		http.Error(w, "Too many requests", http.StatusTooManyRequests)
		return
	}

	// Tarpitting to slow down automated scanners
	time.Sleep(200 * time.Millisecond)

	if r.Method != http.MethodGet {
		http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
		return
	}

	encodedToken := strings.TrimPrefix(r.URL.Path, "/api/public/check/")
    if encodedToken == "" {
        http.Error(w, "Token required", http.StatusBadRequest)
        return
    }
	
	token, err := url.PathUnescape(encodedToken)
	if err != nil {
		http.Error(w, "Invalid token encoding", http.StatusBadRequest)
		return
	}
	
	if len(token) > 32 {
		http.Error(w, "Invalid token", http.StatusBadRequest)
		return
	}

    active, err := checkSynapseTokenActive(token)
    if err != nil {
        log.Printf("Error checking token [%s]: %v", token, err)
		active = false
    }

    w.Header().Set("Content-Type", "application/json")
	w.Header().Set("Cache-Control", "no-store, no-cache, must-revalidate")
    json.NewEncoder(w).Encode(map[string]bool{"active": active})
}

// handleTokens manages user-specific tokens (listing and creating)
func handleTokens(w http.ResponseWriter, r *http.Request) {
	userID, err := authenticate(r)
	if err != nil {
		http.Error(w, "Unauthorized", http.StatusUnauthorized)
		return
	}

	if r.Method == http.MethodGet {
		tokens, err := getActiveTokens(userID)
		if err != nil {
			http.Error(w, err.Error(), http.StatusInternalServerError)
			return
		}
		json.NewEncoder(w).Encode(map[string]interface{}{"tokens": tokens})
		return
	}

	if r.Method == http.MethodPost {
		tokens, err := getActiveTokens(userID)
		if err != nil {
			http.Error(w, err.Error(), http.StatusInternalServerError)
			return
		}

		if len(tokens) >= invitesLimit {
			http.Error(w, "Invites limit", http.StatusBadRequest)
			return
		}

		tokenStr, err := createSynapseToken()
		if err != nil {
			http.Error(w, "Failed to create token in Synapse", http.StatusInternalServerError)
			return
		}

		_, err = db.Exec("INSERT INTO tokens (user_id, token) VALUES (?, ?)", userID, tokenStr)
		if err != nil {
			http.Error(w, "Failed to save token", http.StatusInternalServerError)
			return
		}

		w.WriteHeader(http.StatusCreated)
		return
	}

	http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
}

// handleDeleteToken handles registration token deletion requests
func handleDeleteToken(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodDelete {
		http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
		return
	}

	userID, err := authenticate(r)
	if err != nil {
		http.Error(w, "Unauthorized", http.StatusUnauthorized)
		return
	}

	token := strings.TrimPrefix(r.URL.Path, "/api/tokens/")
	
	var dbUser string
	err = db.QueryRow("SELECT user_id FROM tokens WHERE token = ? AND deleted = 0", token).Scan(&dbUser)
	if err == sql.ErrNoRows || dbUser != userID {
		http.Error(w, "Forbidden", http.StatusForbidden)
		return
	}

	safeToken := url.PathEscape(token)
	apiURL := fmt.Sprintf("%s/_synapse/admin/v1/registration_tokens/%s", synapseURL, safeToken)
	
	req, err := http.NewRequest(http.MethodDelete, apiURL, nil)
	if err != nil {
		http.Error(w, "Forbidden", http.StatusForbidden)
		return
	}
	admToken, err := waitForAdminToken()
	if err != nil {
		http.Error(w, "Auth error", http.StatusInternalServerError)
		return
	}
	req.Header.Set("Authorization", "Bearer "+admToken)
	resp, err := httpClient.Do(req)
	
	if err != nil {
		http.Error(w, "Upstream error", http.StatusBadGateway)
		return
	}
	defer resp.Body.Close()

	if resp.StatusCode >= 300 {
		http.Error(w, "Failed to delete token", http.StatusBadGateway)
		return
	}
	_, err = db.Exec("UPDATE tokens SET deleted = 1 WHERE token = ?", token)
	if err != nil {
		log.Println("DB delete error:", err)
		return
	}
	w.WriteHeader(http.StatusOK)
	
}

// getActiveTokens returns a list of active tokens for a user, cleaning up stale ones
func getActiveTokens(userID string) ([]string, error) {
	rows, err := db.Query("SELECT token FROM tokens WHERE user_id = ? and deleted = 0", userID)
	if err != nil {
		return nil, err
	}
	
	var allTokens []string
	for rows.Next() {
		var t string
		if err := rows.Scan(&t); err == nil {
			allTokens = append(allTokens, t)
		}
	}
	rows.Close()

	var activeTokens []string
	for _, t := range allTokens {
		isActive, err := checkSynapseTokenActive(t)
		if err != nil {
			log.Printf("Cannot check token %s: %v", t, err)
			activeTokens = append(activeTokens, t)
			continue
		}
		
		if !isActive {
			db.Exec("UPDATE tokens SET deleted = 1 WHERE token = ?", t)
			continue
		}
		activeTokens = append(activeTokens, t)
	}
	return activeTokens, nil
}

// checkSynapseTokenActive validates if a token is still usable via Synapse Admin API
func checkSynapseTokenActive(token string) (bool, error) {
	safeToken := url.PathEscape(token)
	apiURL := fmt.Sprintf("%s/_synapse/admin/v1/registration_tokens/%s", synapseURL, safeToken)
	
	req, err := http.NewRequest(http.MethodGet, apiURL, nil)
	if err != nil {
		return false, err
	}
	
	admToken, err := waitForAdminToken()
	if err != nil {
		return false, err
	}
	req.Header.Set("Authorization", "Bearer "+admToken)

	resp, err := httpClient.Do(req)
	if err != nil {
		return false, err
	}
	defer resp.Body.Close()

	if resp.StatusCode == http.StatusNotFound {
		return false, nil
	}

	var result struct {
		UsesAllowed *int   `json:"uses_allowed"`
		Completed   int    `json:"completed"`
		ExpiryTime  *int64 `json:"expiry_time"`
	}
	decoder := json.NewDecoder(io.LimitReader(resp.Body, 1<<20)) // 1MB
	if err := decoder.Decode(&result); err != nil {
		return false, err
	}

	if result.UsesAllowed != nil {
		if result.Completed >= *result.UsesAllowed {
			return false, nil
		}
	}

	if result.ExpiryTime != nil {
		nowMs := time.Now().UnixNano() / int64(time.Millisecond)
		if nowMs >= *result.ExpiryTime {
			return false, nil
		}
	}

	return true, nil
}

// createSynapseToken creates a single-use registration token in Synapse
func createSynapseToken() (string, error) {
	// uses_allowed: 1 makes token one-usable
	reqBody := bytes.NewBufferString(`{"uses_allowed": 1}`)
	req, err := http.NewRequest(http.MethodPost, synapseURL+"/_synapse/admin/v1/registration_tokens/new", reqBody)
	if err != nil {
		return "", err // или http.Error(...)
	}
	admToken, err := waitForAdminToken()
	if err != nil {
		return "", err
	}
	req.Header.Set("Authorization", "Bearer "+admToken)
	req.Header.Set("Content-Type", "application/json")

	resp, err := httpClient.Do(req)
	if err != nil {
		return "", err
	}
	defer resp.Body.Close()

	if resp.StatusCode >= 400 {
		return "", fmt.Errorf("synapse API error")
	}

	var result struct {
		Token string `json:"token"`
	}
	decoder := json.NewDecoder(io.LimitReader(resp.Body, 1<<20)) // 1MB
	if err := decoder.Decode(&result); err != nil {
		return "", err
	}

	return result.Token, nil
}

// isRateLimited returns true if an IP has made requests too frequently
func isRateLimited(ip string) bool {
	mu.Lock()
	defer mu.Unlock()
	
	parsed := net.ParseIP(ip)
	
	if parsed == nil {
		return true
	}
	ip = parsed.String()

	v, exists := visitors[ip]
	if !exists {
		visitors[ip] = &ipLimiter{lastSeen: time.Now()}
		return false
	}

	if time.Since(v.lastSeen) < 2*time.Second {
		v.lastSeen = time.Now()
		return true
	}

	v.lastSeen = time.Now()
	return false
}

// cleanupVisitors removes stale entries from the rate limit map
func cleanupVisitors() {
	for {
		time.Sleep(1 * time.Hour)
		mu.Lock()
		for ip, v := range visitors {
			if time.Since(v.lastSeen) > 1*time.Hour {
				delete(visitors, ip)
			}
		}
		mu.Unlock()
	}
}
