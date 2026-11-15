//go:build android

package tsbridge

import "tailscale.com/net/netmon"

// SetDefaultInterface supplies Android's active route to Tailscale.
func SetDefaultInterface(name string) { netmon.UpdateLastKnownDefaultRouteInterface(name) }
