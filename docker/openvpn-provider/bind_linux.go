//go:build linux

package main

import "golang.org/x/sys/unix"

func bindToDevice(fd int, iface string) error {
	return unix.SetsockoptString(fd, unix.SOL_SOCKET, unix.SO_BINDTODEVICE, iface)
}

