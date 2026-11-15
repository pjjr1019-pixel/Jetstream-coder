"""Application composition for a locally approved embedded connection.

This staged setup must not be installed until the socket/discovery acceptance
checks and the bridge/Pad startup and shutdown integration have passed.
"""
from pathlib import Path
from companion_data import paired_certificates, stream_root
from embedded_firewall import existing_rules_verified
from local_private_acl import verify_private
from owned_video_node import OwnedVideoNode
from peer_binding import BindingStore
from sunshine_controller_factory import make_controller
from sunshine_socket_preflight import check_ready
from .embedded_lifecycle import EmbeddedLifecycle
from .embedded_manager import EmbeddedManager


def build_lifecycle(bridge):
    root=Path(stream_root()).resolve(strict=True)
    private=root/'private'

    def make_manager(binding,on_sign_in,on_address):
        return EmbeddedManager(
            bridge,binding,private,OwnedVideoNode,
            lambda:make_controller(bridge,root,verify_private),
            verify_private,
            lambda controller:check_ready(controller,lambda:existing_rules_verified(root)),
            on_sign_in,on_address,
        )

    def persist(binding):
        if verify_private(private) is not True:
            raise PermissionError('Private connection storage unavailable')
        store=BindingStore(private,private_acl_verified=True)
        store.approve_local({'node':binding['node'],'address':binding['address']},
                            binding['fingerprint'],set(paired_certificates()),
                            locally_approved=True,identity_verified=True)

    return EmbeddedLifecycle(make_manager,persist)
