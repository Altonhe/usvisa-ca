package main

import (
	"log"
	"os"
	"strings"
)

type Config struct {
	ConfigDir      string
	ConfigName     string
	AuthFile       string
	TunInterface   string
	SocksAddr      string
	ControlAddr    string
	OpenVPNBinary  string
	EgressProbeURL string
}

func main() {
	cfg := Config{
		ConfigDir:      env("OPENVPN_CONFIG_DIR", "/vpn"),
		ConfigName:     env("OPENVPN_CONFIG_NAME", ""),
		AuthFile:       env("OPENVPN_AUTH_FILE", "/run/secrets/openvpn_auth"),
		TunInterface:   env("OPENVPN_TUN_INTERFACE", "tun0"),
		SocksAddr:      env("SOCKS_ADDR", ":1080"),
		ControlAddr:    env("CONTROL_ADDR", ":8080"),
		OpenVPNBinary:  env("OPENVPN_BIN", "openvpn"),
		EgressProbeURL: env("EGRESS_PROBE_URL", "https://api.ipify.org?format=json"),
	}

	provider := &Provider{cfg: cfg, status: "initializing"}
	if err := provider.Start(cfg.ConfigName); err != nil {
		log.Printf("initial OpenVPN start failed: %v", err)
	}

	go downloadConfigs(cfg.ConfigDir, provider.StartIfIdle)
	go provider.serveSOCKS()

	if err := provider.serveControl(); err != nil {
		log.Fatalf("control server failed: %v", err)
	}
}

func env(key string, fallback string) string {
	value := strings.TrimSpace(os.Getenv(key))
	if value == "" {
		return fallback
	}
	return value
}
