package main

import (
	"archive/zip"
	"bytes"
	"fmt"
	"io"
	"log"
	"net/http"
	"os"
	"path"
	"path/filepath"
	"strings"
	"time"
)

const nordVPNConfigURL = "https://downloads.nordcdn.com/configs/archives/servers/ovpn.zip"

var allowedNordVPNServers = map[string]struct{}{
	"ca2102": {}, "ca2227": {}, "ca2232": {}, "ca2197": {},
	"ca2223": {}, "ca2122": {}, "ca2096": {}, "ca2229": {},
	"us12895": {}, "us13585": {}, "us12972": {}, "us12956": {},
	"us12898": {}, "us13547": {}, "us13569": {}, "us13561": {},
}

func allowedNordVPNProfile(name string) bool {
	server, suffix, found := strings.Cut(strings.ToLower(name), ".nordvpn.com.")
	if !found {
		return false
	}
	if _, allowed := allowedNordVPNServers[server]; !allowed {
		return false
	}
	switch suffix {
	case "udp.ovpn", "udp_2.6.ovpn", "tcp.ovpn", "tcp_2.6.ovpn":
		return true
	default:
		return false
	}
}

func configCategory(name string) string {
	baseName := strings.ToLower(path.Base(name))
	if !allowedNordVPNProfile(baseName) {
		return ""
	}
	var proto string
	switch path.Base(path.Dir(name)) {
	case "ovpn_udp":
		proto = "udp"
	case "ovpn_tcp":
		proto = "tcp"
	default:
		return ""
	}
	if !strings.HasPrefix(strings.SplitN(baseName, ".nordvpn.com.", 2)[1], proto) {
		return ""
	}
	var country string
	switch {
	case strings.HasPrefix(baseName, "us"):
		country = "us"
	case strings.HasPrefix(baseName, "ca"):
		country = "ca"
	default:
		return ""
	}
	return fmt.Sprintf("%s-%s", country, proto)
}

func downloadConfigs(configDir string) {
	for {
		func() {
			log.Printf("Starting background update of NordVPN configurations from official archive...")

			client := &http.Client{Timeout: 60 * time.Second}
			resp, err := client.Get(nordVPNConfigURL)
			if err != nil {
				log.Printf("Failed to download official NordVPN configs: %v", err)
				return
			}
			defer resp.Body.Close()

			if resp.StatusCode != http.StatusOK {
				log.Printf("Failed to download configs: HTTP status %d", resp.StatusCode)
				return
			}

			bodyBytes, err := io.ReadAll(resp.Body)
			if err != nil {
				log.Printf("Failed to read config zip body: %v", err)
				return
			}

			reader, err := zip.NewReader(bytes.NewReader(bodyBytes), int64(len(bodyBytes)))
			if err != nil {
				log.Printf("Failed to parse config zip: %v", err)
				return
			}

			// Categorize files in the zip
			// Key: category (us-udp, us-tcp, ca-udp, ca-tcp), Value: list of zip files
			categories := map[string][]*zip.File{
				"us-udp": {},
				"us-tcp": {},
				"ca-udp": {},
				"ca-tcp": {},
			}

			available := 0
			for _, file := range reader.File {
				if file.FileInfo().IsDir() {
					continue
				}

				catKey := configCategory(file.Name)
				if catKey == "" {
					continue
				}
				categories[catKey] = append(categories[catKey], file)
				available++
			}
			if available == 0 {
				log.Printf("Official NordVPN archive has no allowlisted profiles; keeping existing configurations")
				return
			}

			// Clean up old downloaded configurations before extracting new ones to avoid infinite accumulation
			if oldEntries, err := os.ReadDir(configDir); err == nil {
				for _, entry := range oldEntries {
					if entry.IsDir() {
						continue
					}
					name := strings.ToLower(entry.Name())
					if strings.HasSuffix(name, ".ovpn") && (strings.HasPrefix(name, "us") || strings.HasPrefix(name, "ca")) {
						_ = os.Remove(filepath.Join(configDir, entry.Name()))
					}
				}
			}

			// Keep every protocol variant of the explicitly allowlisted servers.
			var extractedFiles []string

			for _, files := range categories {
				for _, file := range files {
					baseName := filepath.Base(file.Name)
					destPath := filepath.Join(configDir, baseName)

					srcFile, err := file.Open()
					if err != nil {
						log.Printf("Failed to open zip file %s: %v", file.Name, err)
						continue
					}

					destFile, err := os.OpenFile(destPath, os.O_WRONLY|os.O_CREATE|os.O_TRUNC, 0644)
					if err != nil {
						srcFile.Close()
						log.Printf("Failed to create destination file %s: %v", destPath, err)
						continue
					}

					_, err = io.Copy(destFile, srcFile)
					srcFile.Close()
					destFile.Close()

					if err != nil {
						log.Printf("Failed to write file %s: %v", destPath, err)
					} else {
						extractedFiles = append(extractedFiles, baseName)
					}
				}
			}

			log.Printf("Successfully updated %d NordVPN configurations from official archive (allowlisted servers only).", len(extractedFiles))
		}()

		// Sleep for 24 hours
		time.Sleep(24 * time.Hour)
	}
}
