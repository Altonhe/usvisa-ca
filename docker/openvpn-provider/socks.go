package main

import (
	"context"
	"errors"
	"log"
	"net"
	"syscall"
	"time"

	"github.com/armon/go-socks5"
)

func (p *Provider) serveSOCKS() {
	dialer := func(ctx context.Context, network, addr string) (net.Conn, error) {
		p.mu.Lock()
		status := p.status
		p.mu.Unlock()
		if status != "connected" {
			return nil, errors.New("OpenVPN is not connected")
		}

		dialer := &net.Dialer{
			Timeout: 30 * time.Second,
			Control: func(network string, address string, conn syscall.RawConn) error {
				var controlErr error
				iface := p.cfg.TunInterface
				err := conn.Control(func(fd uintptr) {
					controlErr = bindToDevice(int(fd), iface)
				})
				if err != nil {
					return err
				}
				return controlErr
			},
		}
		return dialer.DialContext(ctx, network, addr)
	}

	server, err := socks5.New(&socks5.Config{Dial: dialer})
	if err != nil {
		log.Fatalf("create socks server: %v", err)
	}

	log.Printf("SOCKS5 listening on %s", p.cfg.SocksAddr)
	if err := server.ListenAndServe("tcp", p.cfg.SocksAddr); err != nil {
		log.Fatalf("socks server failed: %v", err)
	}
}
