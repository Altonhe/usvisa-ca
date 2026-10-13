//go:build !linux

package main

func bindToDevice(fd int, iface string) error {
	return nil
}

