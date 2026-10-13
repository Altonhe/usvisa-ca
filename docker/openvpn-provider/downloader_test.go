package main

import (
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
)

func TestConfigCategory(t *testing.T) {
	for _, test := range []struct {
		name string
		want string
	}{
		{"ovpn_udp/ca2102.nordvpn.com.udp.ovpn", "ca-udp"},
		{"ovpn_udp/ca2102.nordvpn.com.udp_2.6.ovpn", "ca-udp"},
		{"ovpn_tcp/ca2102.nordvpn.com.tcp.ovpn", "ca-tcp"},
		{"ovpn_tcp/us12895.nordvpn.com.tcp_2.6.ovpn", "us-tcp"},
		{"ovpn_udp/us12895.nordvpn.com.udp.ovpn", "us-udp"},
		{"wrapper/ovpn_udp/ca2102.nordvpn.com.udp.ovpn", "ca-udp"},
		{"ovpn_udp/CA2102.nordvpn.com.udp.OVPN", "ca-udp"},
		{"ovpn_udp/ca1066.nordvpn.com.udp.ovpn", ""},
		{"ovpn_udp/us1234.nordvpn.com.udp.ovpn", ""},
		{"ovpn_udp/ca21020.nordvpn.com.udp.ovpn", ""},
		{"ovpn_tcp/ca2102.nordvpn.com.udp.ovpn", ""},
		{"ovpn_udp/de1234.nordvpn.com.udp.ovpn", ""},
		{"ovpn_udp/ca1066.txt", ""},
		{"unknown/ca1066.nordvpn.com.udp.ovpn", ""},
		{"ca1066.nordvpn.com.udp.ovpn", ""},
		{"ovpn_udp/", ""},
	} {
		t.Run(test.name, func(t *testing.T) {
			if got := configCategory(test.name); got != test.want {
				t.Errorf("configCategory(%q) = %q, want %q", test.name, got, test.want)
			}
		})
	}
}

func TestAllowedNordVPNProfiles(t *testing.T) {
	servers := []string{
		"ca2102", "ca2227", "ca2232", "ca2197", "ca2223", "ca2122", "ca2096", "ca2229",
		"us12895", "us13585", "us12972", "us12956", "us12898", "us13547", "us13569", "us13561",
	}
	if len(allowedNordVPNServers) != len(servers) {
		t.Fatalf("got %d allowlisted servers, want %d", len(allowedNordVPNServers), len(servers))
	}
	for _, server := range servers {
		for _, protocol := range []string{"udp", "tcp"} {
			for _, version := range []string{"", "_2.6"} {
				name := server + ".nordvpn.com." + protocol + version + ".ovpn"
				if !allowedNordVPNProfile(name) {
					t.Errorf("allowlisted profile rejected: %s", name)
				}
				if got := configCategory("ovpn_" + protocol + "/" + name); got != server[:2]+"-"+protocol {
					t.Errorf("unexpected category %q for %s", got, name)
				}
			}
		}
	}
	for _, name := range []string{
		"client.ovpn", "ca2102.nordvpn.com.evil.udp.ovpn", "ca2102.nordvpn.com.udp.ovpn.bak",
		"ca2102.nordvpn.com.udp_2.7.ovpn", "ca2102.evil.com.udp.ovpn",
		"../ca2102.nordvpn.com.udp.ovpn",
	} {
		if allowedNordVPNProfile(name) {
			t.Errorf("unexpectedly allowed profile: %s", name)
		}
	}
}

func TestProviderListsAndSwitchesOnlyAllowlistedProfiles(t *testing.T) {
	dir := t.TempDir()
	for _, name := range []string{
		"ca2102.nordvpn.com.udp.ovpn", "us12895.nordvpn.com.tcp_2.6.ovpn",
		"ca1066.nordvpn.com.udp.ovpn", "client.ovpn", "us1234.nordvpn.com.udp.ovpn",
	} {
		if err := os.WriteFile(filepath.Join(dir, name), []byte("test profile"), 0644); err != nil {
			t.Fatal(err)
		}
	}
	p := &Provider{cfg: Config{ConfigDir: dir, OpenVPNBinary: "must-not-run"}}
	profiles, err := p.Profiles()
	if err != nil {
		t.Fatal(err)
	}
	want := []string{"ca2102.nordvpn.com.udp.ovpn", "us12895.nordvpn.com.tcp_2.6.ovpn"}
	if !reflect.DeepEqual(profiles, want) {
		t.Fatalf("profiles = %v, want %v", profiles, want)
	}
	for _, name := range []string{"ca1066.nordvpn.com.udp.ovpn", "client.ovpn"} {
		if err := p.Switch(SwitchRequest{Profile: name}); err == nil || !strings.Contains(err.Error(), "unknown OpenVPN profile") {
			t.Errorf("switch to %s should reject before starting OpenVPN, got %v", name, err)
		}
	}
	if p.Status().AvailableCount != len(want) || p.switches != 0 {
		t.Fatal("provider included or started a non-allowlisted profile")
	}
}
