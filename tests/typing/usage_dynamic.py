"""Static checks of the dynamic proxy: mypy --strict reads this file, pytest never collects it.

Attribute access belongs to the module, so the identity built-ins (and any name the proxy
does not define) are methods. The ``type: ignore`` lines are negative assertions.
"""

from __future__ import annotations

from typing_extensions import assert_type

from logos_bridge import AsyncBridgeClient, BridgeClient, ModuleInfo
from logos_bridge.dynamic import BlockingDynamicMethod, BlockingDynamicModule, DynamicMethod, DynamicModule
from logos_bridge.lidl import Interface


async def usage(bridge: AsyncBridgeClient, blocking: BridgeClient) -> None:
    proxy = await bridge.module("m")
    assert_type(proxy, DynamicModule)
    assert_type(proxy.name, DynamicMethod)
    assert_type(proxy.version, DynamicMethod)
    assert_type(proxy.lidl, DynamicMethod)
    assert_type(proxy.status, DynamicMethod)
    assert_type(proxy["events"], DynamicMethod)
    assert_type(proxy.module_name, str)
    assert_type(proxy.module_info, ModuleInfo)
    assert_type(proxy.interface_status, str | None)
    assert_type(proxy.is_typed, bool)
    assert_type(proxy.served_interface, Interface | None)
    assert_type(proxy.bridge_client, AsyncBridgeClient)
    assert_type(proxy.exposed_methods, tuple[str, ...])
    assert_type(await proxy.refreshed(), DynamicModule)
    proxy.module_name()  # type: ignore[operator]

    twin = blocking.module("m")
    assert_type(twin, BlockingDynamicModule)
    assert_type(twin.name, BlockingDynamicMethod)
    assert_type(twin.version, BlockingDynamicMethod)
    assert_type(twin.lidl, BlockingDynamicMethod)
    assert_type(twin["on"], BlockingDynamicMethod)
    assert_type(twin.module_name, str)
    assert_type(twin.bridge_client, BridgeClient)
    assert_type(twin.aio, DynamicModule)
    assert_type(twin.refreshed(), BlockingDynamicModule)
    twin.is_typed()  # type: ignore[operator]
