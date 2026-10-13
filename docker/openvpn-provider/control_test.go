package main

import (
	"net/http"
	"net/http/httptest"
	"testing"
)

func TestHealthReflectsOpenVPNStatus(t *testing.T) {
	p := &Provider{cfg: Config{ConfigDir: t.TempDir()}, status: "error"}
	for _, test := range []struct {
		status string
		want   int
	}{
		{"error", http.StatusServiceUnavailable},
		{"connecting", http.StatusServiceUnavailable},
		{"connected", http.StatusOK},
	} {
		p.status = test.status
		response := httptest.NewRecorder()
		p.controlMux().ServeHTTP(response, httptest.NewRequest(http.MethodGet, "/health", nil))
		if response.Code != test.want {
			t.Errorf("status %q: got %d, want %d", test.status, response.Code, test.want)
		}
	}
}
