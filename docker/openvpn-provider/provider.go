package main

import (
	"encoding/json"
	"errors"
	"fmt"
	"log"
	"net"
	"os"
	"os/exec"
	"path/filepath"
	"sort"
	"strings"
	"sync"
	"syscall"
	"time"
)

type Provider struct {
	cfg Config

	mu       sync.Mutex
	cmd      *exec.Cmd
	waitChan chan struct{}
	active   string
	status   string
	lastErr  string
	started  time.Time
	switches int
	tmpAuth  string // temp auth file path (LF-normalized), cleaned up on stop
}

type Status struct {
	Status         string    `json:"status"`
	ActiveProfile  string    `json:"activeProfile"`
	ConfigDir      string    `json:"configDir"`
	TunInterface   string    `json:"tunInterface"`
	SocksAddr      string    `json:"socksAddr"`
	ControlAddr    string    `json:"controlAddr"`
	StartedAt      time.Time `json:"startedAt,omitempty"`
	SwitchCount    int       `json:"switchCount"`
	LastError      string    `json:"lastError,omitempty"`
	AvailableCount int       `json:"availableCount"`
}

type SwitchRequest struct {
	Profile string `json:"profile"`
	Random  bool   `json:"random"`
}

func (p *Provider) Start(profile string) error {
	p.mu.Lock()
	defer p.mu.Unlock()

	profiles, err := p.profilesLocked()
	if err != nil {
		return p.setErrorLocked(err)
	}
	if len(profiles) == 0 {
		return p.setErrorLocked(fmt.Errorf("no allowlisted NordVPN profiles in %s", p.cfg.ConfigDir))
	}

	selected := profile
	if selected == "" {
		selected = profiles[0]
	}
	if selected == "random" {
		selected = profiles[int(time.Now().UnixNano()%int64(len(profiles)))]
	}
	if !contains(profiles, selected) {
		return p.setErrorLocked(fmt.Errorf("unknown OpenVPN profile: %s", selected))
	}

	p.stopLocked()

	configPath := filepath.Join(p.cfg.ConfigDir, selected)
	args := []string{"--config", configPath}
	authPath, authErr := p.resolveAuthFileLocked()
	if authErr != nil {
		log.Printf("auth file warning: %v", authErr)
	} else if authPath != "" {
		args = append(args, "--auth-user-pass", authPath)
	}

	cmd := exec.Command(p.cfg.OpenVPNBinary, args...)
	cmd.Stdout = os.Stdout
	cmd.Stderr = os.Stderr

	if err := cmd.Start(); err != nil {
		return p.setErrorLocked(fmt.Errorf("start OpenVPN: %w", err))
	}

	p.cmd = cmd
	p.waitChan = make(chan struct{})
	p.active = selected
	p.status = "connecting"
	p.lastErr = ""
	p.started = time.Now()
	p.switches++

	waitChan := p.waitChan
	go func() {
		err := cmd.Wait()
		close(waitChan)

		p.mu.Lock()
		defer p.mu.Unlock()
		if p.cmd == cmd {
			p.status = "error"
			if err != nil {
				p.lastErr = fmt.Sprintf("openvpn exited: %v", err)
			} else {
				p.lastErr = "openvpn process exited unexpectedly (exit code 0); inspect OpenVPN logs"
			}
			log.Printf("OpenVPN process exited for profile %s: %s", selected, p.lastErr)
			p.cmd = nil
		}
	}()

	go func() {
		if err := p.waitForTun(cmd, 60*time.Second); err != nil {
			p.mu.Lock()
			if p.cmd == cmd {
				_ = p.setErrorLocked(err)
			}
			p.mu.Unlock()
			return
		}
		p.mu.Lock()
		if p.cmd == cmd {
			p.status = "connected"
			p.lastErr = ""
		}
		p.mu.Unlock()
	}()

	return nil
}

func (p *Provider) Switch(req SwitchRequest) error {
	profile := req.Profile
	if req.Random {
		profile = "random"
	}
	return p.Start(profile)
}

// StartIfIdle starts OpenVPN on the configured profile unless it is already
// connected or connecting. Called after the profile download, which on a
// fresh volume finishes after the initial Start has already failed.
func (p *Provider) StartIfIdle() {
	p.mu.Lock()
	running := p.cmd != nil && (p.status == "connected" || p.status == "connecting")
	p.mu.Unlock()
	if running {
		return
	}
	log.Printf("profiles available; starting OpenVPN")
	if err := p.Start(p.cfg.ConfigName); err != nil {
		log.Printf("OpenVPN start after profile update failed: %v", err)
	}
}

func (p *Provider) Status() Status {
	p.mu.Lock()
	defer p.mu.Unlock()

	profiles, _ := p.profilesLocked()
	return Status{
		Status:         p.status,
		ActiveProfile:  p.active,
		ConfigDir:      p.cfg.ConfigDir,
		TunInterface:   p.cfg.TunInterface,
		SocksAddr:      p.cfg.SocksAddr,
		ControlAddr:    p.cfg.ControlAddr,
		StartedAt:      p.started,
		SwitchCount:    p.switches,
		LastError:      p.lastErr,
		AvailableCount: len(profiles),
	}
}

func (p *Provider) Profiles() ([]string, error) {
	p.mu.Lock()
	defer p.mu.Unlock()
	return p.profilesLocked()
}

func (p *Provider) profilesLocked() ([]string, error) {
	entries, err := os.ReadDir(p.cfg.ConfigDir)
	if err != nil {
		return nil, err
	}
	profiles := make([]string, 0)
	for _, entry := range entries {
		if entry.IsDir() {
			continue
		}
		name := entry.Name()
		if allowedNordVPNProfile(name) {
			profiles = append(profiles, name)
		}
	}
	sort.Strings(profiles)
	return profiles, nil
}

func (p *Provider) stopLocked() {
	if p.cmd == nil || p.cmd.Process == nil {
		return
	}

	_ = p.cmd.Process.Signal(syscall.SIGTERM)
	waitChan := p.waitChan

	select {
	case <-waitChan:
	case <-time.After(8 * time.Second):
		if p.cmd != nil && p.cmd.Process != nil {
			_ = p.cmd.Process.Kill()
		}
		<-waitChan
	}
	p.cmd = nil
	p.waitChan = nil

	if p.tmpAuth != "" {
		_ = os.Remove(p.tmpAuth)
		p.tmpAuth = ""
	}
}

func (p *Provider) resolveAuthFileLocked() (string, error) {
	if _, err := os.Stat(p.cfg.AuthFile); err != nil {
		return "", nil
	}
	raw, err := os.ReadFile(p.cfg.AuthFile)
	if err != nil {
		return "", fmt.Errorf("read auth file: %w", err)
	}

	var username, password string
	content := strings.TrimSpace(string(raw))

	if strings.HasPrefix(content, "{") {
		var creds struct {
			Username string `json:"username"`
			Password string `json:"password"`
		}
		if err := json.Unmarshal([]byte(content), &creds); err != nil {
			return "", fmt.Errorf("parse JSON auth file: %w", err)
		}
		username, password = creds.Username, creds.Password
	} else {
		lines := strings.Split(content, "\n")
		if len(lines) < 2 {
			return "", fmt.Errorf("auth file must contain username on line 1 and password on line 2")
		}
		username = strings.TrimRight(lines[0], "\r")
		password = strings.TrimRight(lines[1], "\r")
	}

	if username == "" || password == "" {
		return "", fmt.Errorf("auth file: username or password is empty")
	}

	tmp, err := os.CreateTemp("", "openvpn-auth-*.txt")
	if err != nil {
		return "", fmt.Errorf("create temp auth file: %w", err)
	}
	_, _ = fmt.Fprintf(tmp, "%s\n%s\n", username, password)
	_ = tmp.Close()

	log.Printf("Successfully loaded OpenVPN credentials for user %q (password length: %d) to %s", username, len(password), tmp.Name())

	p.tmpAuth = tmp.Name()
	return p.tmpAuth, nil
}

func (p *Provider) setErrorLocked(err error) error {
	p.status = "error"
	p.lastErr = err.Error()
	return err
}

func (p *Provider) waitForTun(cmd *exec.Cmd, timeout time.Duration) error {
	deadline := time.Now().Add(timeout)
	for time.Now().Before(deadline) {
		p.mu.Lock()
		currentCmd := p.cmd
		p.mu.Unlock()
		if currentCmd != cmd {
			return errors.New("OpenVPN process replaced or stopped")
		}

		iface, err := net.InterfaceByName(p.cfg.TunInterface)
		if err == nil && iface.Flags&net.FlagUp != 0 {
			return nil
		}
		time.Sleep(1 * time.Second)
	}
	return fmt.Errorf("timed out waiting for %s", p.cfg.TunInterface)
}

func contains(items []string, target string) bool {
	for _, item := range items {
		if item == target {
			return true
		}
	}
	return false
}
