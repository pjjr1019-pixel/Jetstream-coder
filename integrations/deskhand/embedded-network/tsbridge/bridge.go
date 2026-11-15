// Package tsbridge is an isolated Android startup probe, not a Deskhand transport.
package tsbridge

import (
	"context"
	"crypto/rand"
	"crypto/subtle"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/netip"
	"os"
	"sync"
	"time"

	"tailscale.com/net/netmon"
	"tailscale.com/tsnet"
)

var mu sync.Mutex
var node *tsnet.Server
var forwards []net.Listener
var forwardError string

func LastForwardError() string { mu.Lock(); defer mu.Unlock(); return forwardError }

// Interfaces supplies current Android ConnectivityManager data to netmon.
type Interfaces interface { Snapshot() (string, error) }
type iface struct { Name string; Index, MTU int; Prefixes []string }

func SetInterfaces(source Interfaces) error {
	if source == nil { return errors.New("interface source required") }
	netmon.RegisterInterfaceGetter(func() ([]netmon.Interface, error) {
		data, err := source.Snapshot(); if err != nil { return nil, err }
		var entries []iface
		if err := json.Unmarshal([]byte(data), &entries); err != nil { return nil, err }
		out := make([]netmon.Interface, 0, len(entries))
		for _, entry := range entries {
			if entry.Name == "" || entry.Index <= 0 { continue }
			item := netmon.Interface{Interface: &net.Interface{Name: entry.Name, Index: entry.Index, MTU: entry.MTU, Flags: net.FlagUp}, AltAddrs: []net.Addr{}}
			for _, text := range entry.Prefixes {
				p, err := netip.ParsePrefix(text); if err != nil { return nil, err }
				item.AltAddrs = append(item.AltAddrs, &net.IPNet{IP: net.IP(p.Addr().AsSlice()), Mask: net.CIDRMask(p.Bits(), p.Addr().BitLen())})
			}
			out = append(out, item)
		}
		if len(out) == 0 { return nil, errors.New("no Android network interfaces") }
		return out, nil
	})
	return nil
}

// Start creates a test node using a directory supplied by the Android app.
// It never accepts or logs an authentication key.
func Start(stateDir string) error {
	mu.Lock()
	defer mu.Unlock()
	if node != nil || stateDir == "" {
		return errors.New("invalid probe state")
	}
	if err := os.MkdirAll(stateDir, 0700); err != nil { return err }
	if err := os.Setenv("TMPDIR", stateDir); err != nil { return err }
	s := &tsnet.Server{Dir: stateDir, Hostname: "deskhand-android-test", UserLogf: func(string, ...any) {}}
	if err := s.Start(); err != nil {
		_ = s.Close()
		return err
	}
	node = s
	return nil
}

// StatusJSON returns only the state and sign-in URL, to be shown in the app.
// The caller must not log the returned value.
func StatusJSON() (string, error) {
	mu.Lock(); s := node; mu.Unlock()
	if s == nil { return "", errors.New("node not started") }
	client, err := s.LocalClient(); if err != nil { return "", err }
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second); defer cancel()
	status, err := client.StatusWithoutPeers(ctx); if err != nil { return "", err }
	data, err := json.Marshal(struct { State string `json:"state"`; AuthURL string `json:"authUrl"` }{status.BackendState, status.AuthURL})
	return string(data), err
}

// ForwardJSON creates an authenticated loopback TCP forward to one tailnet IP.
// The caller must keep the returned token private and never log it.
func ForwardJSON(host string, port int) (string, error) {
	ip, err := netip.ParseAddr(host)
	if err != nil || !ip.Is4() || !netip.MustParsePrefix("100.64.0.0/10").Contains(ip) || port < 1 || port > 65535 {
		return "", errors.New("invalid tailnet destination")
	}
	mu.Lock(); defer mu.Unlock()
	if node == nil { return "", errors.New("node not started") }
	listener, err := net.Listen("tcp4", "127.0.0.1:0"); if err != nil { return "", err }
	secret := make([]byte, 32); if _, err = rand.Read(secret); err != nil { listener.Close(); return "", err }
	token := hex.EncodeToString(secret)
	server := node
	forwards = append(forwards, listener)
	go func() {
		for {
			conn, err := listener.Accept(); if err != nil { return }
			go func() {
				defer conn.Close()
				_ = conn.SetReadDeadline(time.Now().Add(5*time.Second))
				presented := make([]byte, len(token)); if _, err := io.ReadFull(conn, presented); err != nil || subtle.ConstantTimeCompare(presented, []byte(token)) != 1 { return }
				_ = conn.SetReadDeadline(time.Time{})
				ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second); defer cancel()
				remote, err := server.Dial(ctx, "tcp", net.JoinHostPort(host, fmt.Sprint(port))); if err != nil { mu.Lock(); forwardError = err.Error(); mu.Unlock(); return }
				defer remote.Close()
				done := make(chan struct{}, 1)
				go func() { _, _ = io.Copy(remote, conn); done <- struct{}{} }()
				_, _ = io.Copy(conn, remote)
				_ = remote.Close(); _ = conn.Close(); <-done
			}()
		}
	}()
	data, err := json.Marshal(struct { Port int `json:"port"`; Token string `json:"token"` }{listener.Addr().(*net.TCPAddr).Port, token})
	return string(data), err
}

func Stop() error {
	mu.Lock()
	defer mu.Unlock()
	if node == nil { return nil }
	for _, listener := range forwards { _ = listener.Close() }; forwards = nil
	s := node
	node = nil
	return s.Close()
}
