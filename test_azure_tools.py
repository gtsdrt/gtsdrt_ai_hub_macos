#!/usr/bin/env python3
"""
Azure 工具集测试（21 个工具，全部用 mock client，不打真实 Azure）。

覆盖：17 个新工具的调用路径与返回解析、必填参数校验、subscription_id 覆盖、
监控类统一返回格式、KQL 前 100 条截断、结果 >100KB 截断、
SCHEMAS 完整性、default_schemas() 收集与 /api/health 工具总数。

运行：
  .venv/bin/python test_azure_tools.py
"""

from __future__ import annotations

import datetime
import json
import os
import sys
import unittest

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)

os.environ.setdefault("AICHAT_DB_PATH", "/tmp/aichat-azure-test.db")

import tools  # noqa: E402
from tools import azure_tools  # noqa: E402


# ---------------------------------------------------------------- mock SDK


class FakeNode:
    """可无限级联的假 SDK 节点：node.a.b.c(...) 会记录路径与参数"""

    def __init__(self, sdk: "FakeSDK", path: str = ""):
        self._sdk = sdk
        self._path = path

    def __getattr__(self, name: str) -> "FakeNode":
        return FakeNode(self._sdk, f"{self._path}.{name}" if self._path else name)

    def __call__(self, *args, **kwargs):
        self._sdk.calls.append({"path": self._path, "args": args, "kwargs": kwargs})
        value = self._sdk.results.get(self._path)
        return value() if callable(value) else value


class FakeSDK:
    def __init__(self, results: dict | None = None):
        self.results: dict = results or {}
        self.calls: list[dict] = []

    def last_call(self, path: str) -> dict:
        for call in reversed(self.calls):
            if call["path"] == path:
                return call
        raise AssertionError(f"没有调用过 {path}；实际调用：{[c['path'] for c in self.calls]}")


class FakeClients:
    """替身 AzureClients：属性名与真实类一致，底层是 FakeNode"""

    def __init__(self, sdk: FakeSDK, subscription_id: str):
        self._sdk = sdk
        self.credential = object()
        self.subscription_id = subscription_id

    def _node(self, name: str) -> FakeNode:
        return FakeNode(self._sdk, name)

    network = property(lambda self: self._node("network"))
    compute = property(lambda self: self._node("compute"))
    monitor = property(lambda self: self._node("monitor"))
    storage = property(lambda self: self._node("storage"))
    web = property(lambda self: self._node("web"))
    sql = property(lambda self: self._node("sql"))
    keyvault = property(lambda self: self._node("keyvault"))
    aks = property(lambda self: self._node("aks"))
    aci = property(lambda self: self._node("aci"))
    resource = property(lambda self: self._node("resource"))
    resourcegraph = property(lambda self: self._node("resourcegraph"))

    def resource_id(self, resource_group_name: str, resource_provider: str, resource_name: str) -> str:
        return (
            f"/subscriptions/{self.subscription_id}/resourceGroups/{resource_group_name}"
            f"/providers/{resource_provider}/{resource_name}"
        )


def obj(**kwargs):
    """构造一个只有属性的哑对象（模拟 SDK 模型）"""
    return type("Obj", (), kwargs)()


class AzureToolTestBase(unittest.TestCase):
    subscription_id = "11111111-2222-3333-4444-555555555555"

    def setUp(self) -> None:
        self.sdk = FakeSDK()
        self._original_clients = azure_tools.AzureClients

        def factory(arguments=None):
            arguments = arguments or {}
            return FakeClients(self.sdk, arguments.get("subscription_id") or self.subscription_id)

        azure_tools.AzureClients = factory  # type: ignore[assignment]
        self.addCleanup(lambda: setattr(azure_tools, "AzureClients", self._original_clients))

    def run_tool(self, name: str, arguments: dict) -> dict:
        payload = json.loads(azure_tools.execute(name, arguments))
        self.assertEqual(payload["status"], "success", payload)
        return payload

    def expect_error(self, name: str, arguments: dict) -> str:
        payload = json.loads(azure_tools.execute(name, arguments))
        self.assertEqual(payload["status"], "error", payload)
        self.assertIn("message", payload)
        return payload["message"]


# ---------------------------------------------------------------- 网络类


class NetworkToolTests(AzureToolTestBase):
    def test_01_list_vnets(self) -> None:
        self.sdk.results["network.virtual_networks.list"] = [obj(name="vnet-a"), obj(name="vnet-b")]
        payload = self.run_tool("list_vnets_in_resource_group", {"resource_group_name": "rg1"})
        self.assertEqual(self.sdk.last_call("network.virtual_networks.list")["args"], ("rg1",))
        self.assertEqual(payload["vnets"], ["vnet-a", "vnet-b"])
        self.assertEqual(payload["count"], 2)

    def test_02_get_vnet_details(self) -> None:
        vnet = obj(
            name="vnet-a",
            location="norwayeast",
            address_space=obj(address_prefixes=["10.0.0.0/16"]),
            dhcp_options=obj(dns_servers=["1.1.1.1"]),
            subnets=[
                obj(
                    name="web",
                    address_prefix="10.0.1.0/24",
                    network_security_group=obj(
                        id="/subscriptions/x/resourceGroups/rg1/providers/Microsoft.Network"
                           "/networkSecurityGroups/nsg-web",
                        name="nsg-web",
                    ),
                ),
                obj(name="db", address_prefix="10.0.2.0/24", network_security_group=None),
            ],
        )
        self.sdk.results["network.virtual_networks.get"] = vnet
        payload = self.run_tool(
            "get_vnet_details", {"resource_group_name": "rg1", "vnet_name": "vnet-a"}
        )
        self.assertEqual(
            self.sdk.last_call("network.virtual_networks.get")["args"], ("rg1", "vnet-a")
        )
        self.assertEqual(payload["vnet"]["address_space"], ["10.0.0.0/16"])
        self.assertEqual(payload["vnet"]["dns_servers"], ["1.1.1.1"])
        self.assertEqual(payload["vnet"]["subnets"][0]["nsg"], "nsg-web")
        self.assertIsNone(payload["vnet"]["subnets"][1]["nsg"])

    def test_03_check_subnets_without_nsg(self) -> None:
        self.sdk.results["network.virtual_networks.list"] = [
            obj(name="vnet-a", subnets=[
                obj(name="web", address_prefix="10.0.1.0/24",
                    network_security_group=obj(id="/x/networkSecurityGroups/nsg-web")),
                obj(name="db", address_prefix="10.0.2.0/24", network_security_group=None),
            ]),
            obj(name="vnet-b", subnets=[
                obj(name="app", address_prefix="10.1.0.0/24", network_security_group=None),
            ]),
        ]
        payload = self.run_tool("check_subnets_without_nsg", {"resource_group_name": "rg1"})
        self.assertEqual(
            payload["unprotected"],
            [
                {"vnet": "vnet-a", "subnet": "db", "address_prefix": "10.0.2.0/24"},
                {"vnet": "vnet-b", "subnet": "app", "address_prefix": "10.1.0.0/24"},
            ],
        )

    def test_04_get_subnet_info(self) -> None:
        subnet = obj(
            name="web",
            address_prefix="10.0.1.0/24",
            network_security_group=obj(id="/x/networkSecurityGroups/nsg-web"),
            route_table=obj(id="/x/routeTables/rt1"),
            service_endpoints=[obj(service="Microsoft.Storage")],
            delegations=[obj(service_name="Microsoft.Web/serverFarms")],
        )
        self.sdk.results["network.subnets.get"] = subnet
        payload = self.run_tool(
            "get_subnet_info",
            {"resource_group_name": "rg1", "vnet_name": "vnet-a", "subnet_name": "web"},
        )
        self.assertEqual(
            self.sdk.last_call("network.subnets.get")["args"], ("rg1", "vnet-a", "web")
        )
        self.assertEqual(payload["subnet"]["nsg"], "nsg-web")
        self.assertEqual(payload["subnet"]["route_table"], "/x/routeTables/rt1")
        self.assertEqual(payload["subnet"]["service_endpoints"], ["Microsoft.Storage"])
        self.assertEqual(payload["subnet"]["delegations"], ["Microsoft.Web/serverFarms"])

    def test_05_list_nsgs(self) -> None:
        self.sdk.results["network.network_security_groups.list"] = [obj(name="nsg1"), obj(name="nsg2")]
        payload = self.run_tool("list_nsgs_in_resource_group", {"resource_group_name": "rg1"})
        self.assertEqual(payload["nsgs"], ["nsg1", "nsg2"])


# ---------------------------------------------------------------- 资源清单类


class InventoryToolTests(AzureToolTestBase):
    CASES = [
        ("list_storage_accounts", "storage.storage_accounts.list_by_resource_group", "storage_accounts"),
        ("list_web_apps", "web.web_apps.list_by_resource_group", "web_apps"),
        ("list_sql_servers", "sql.servers.list_by_resource_group", "sql_servers"),
        ("list_keyvaults", "keyvault.vaults.list_by_resource_group", "keyvaults"),
        ("list_aks_clusters", "aks.managed_clusters.list_by_resource_group", "aks_clusters"),
        ("list_container_instances", "aci.container_groups.list_by_resource_group", "container_instances"),
    ]

    def test_06_inventory_tools(self) -> None:
        for tool_name, path, field in self.CASES:
            with self.subTest(tool=tool_name):
                self.sdk.results[path] = [obj(name=f"{field}-1"), obj(name=f"{field}-2")]
                payload = self.run_tool(tool_name, {"resource_group_name": "rg1"})
                self.assertEqual(self.sdk.last_call(path)["args"], ("rg1",))
                self.assertEqual(payload[field], [f"{field}-1", f"{field}-2"])
                self.assertEqual(payload["count"], 2)

    def test_07_inventory_with_subscription_override(self) -> None:
        self.sdk.results["storage.storage_accounts.list_by_resource_group"] = [obj(name="sa1")]
        payload = self.run_tool(
            "list_storage_accounts",
            {"resource_group_name": "rg1", "subscription_id": "sub-override"},
        )
        self.assertEqual(payload["subscription_id"], "sub-override")


# ---------------------------------------------------------------- KQL


class QueryResourcesTests(AzureToolTestBase):
    def test_08_query_resources_small_result(self) -> None:
        rows = [
            {
                "name": "vm1",
                "type": "microsoft.compute/virtualmachines",
                "location": "norwayeast",
                "resourceGroup": "rg1",
                "id": "/subscriptions/x/resourceGroups/rg1/providers/Microsoft.Compute/virtualMachines/vm1",
                "extra": "被忽略",
            },
            {"name": "vm2", "type": "t", "location": "l", "resourceGroup": "rg1", "id": "/id/vm2"},
        ]
        self.sdk.results["resourcegraph.resources"] = obj(data=rows)
        payload = self.run_tool("query_resources", {"kql_query": "resources | project name"})

        request = self.sdk.last_call("resourcegraph.resources")["args"][0]
        self.assertEqual(request.query, "resources | project name")
        self.assertEqual(request.subscriptions, [self.subscription_id])
        self.assertEqual(payload["total"], 2)
        self.assertEqual(payload["count"], 2)
        self.assertEqual(set(payload["results"][0]), {"name", "type", "location", "resourceGroup", "id"})
        self.assertNotIn("note", payload)

    def test_09_query_resources_truncates_to_100(self) -> None:
        rows = [
            {"name": f"vm-{i}", "type": "t", "location": "l", "resourceGroup": "rg", "id": f"/id/{i}"}
            for i in range(150)
        ]
        self.sdk.results["resourcegraph.resources"] = obj(data=rows)
        payload = self.run_tool("query_resources", {"kql_query": "resources"})
        self.assertEqual(payload["total"], 150)
        self.assertEqual(payload["count"], 100)
        self.assertTrue(payload["truncated"])
        self.assertIn("仅返回前 100 条", payload["note"])
        self.assertEqual(payload["results"][0]["name"], "vm-0")
        self.assertEqual(payload["results"][-1]["name"], "vm-99")


# ---------------------------------------------------------------- 监控类


def fake_metrics(metric_name: str = "Percentage CPU", unit: str = "Percent", points: int = 2):
    data = [
        obj(
            time_stamp=datetime.datetime(2026, 9, 21, 8, i),
            average=10.0 + i,
            total=100 + i,
            count=4,
            minimum=5.0,
            maximum=20.0 + i,
        )
        for i in range(points)
    ]
    return obj(value=[obj(name=obj(value=metric_name), unit=unit, timeseries=[obj(data=data)])])


class MetricToolTests(AzureToolTestBase):
    def assert_metric_shape(self, payload: dict) -> None:
        self.assertIn("metrics", payload)
        self.assertTrue(payload["metrics"], "metrics 不应为空")
        for metric in payload["metrics"].values():
            self.assertIn("unit", metric)
            self.assertIn("data", metric)
            for point in metric["data"]:
                self.assertEqual(
                    set(point), {"time", "average", "total", "count", "minimum", "maximum"}
                )

    def test_10_webapp_metrics_defaults(self) -> None:
        self.sdk.results["monitor.metrics.list"] = fake_metrics()
        payload = self.run_tool(
            "get_webapp_metrics", {"resource_group_name": "rg1", "webapp_name": "app1"}
        )
        kwargs = self.sdk.last_call("monitor.metrics.list")["kwargs"]
        self.assertEqual(
            kwargs["resource_uri"],
            f"/subscriptions/{self.subscription_id}/resourceGroups/rg1/providers/Microsoft.Web/sites/app1",
        )
        self.assertEqual(kwargs["metricnames"], azure_tools.DEFAULT_WEBAPP_METRICS)
        self.assertEqual(kwargs["timespan"], "PT1H")
        self.assertEqual(kwargs["interval"], "PT5M")
        self.assertEqual(kwargs["aggregation"], "Average,Total,Count")
        self.assert_metric_shape(payload)
        self.assertEqual(payload["metrics"]["Percentage CPU"]["unit"], "Percent")
        self.assertEqual(payload["metrics"]["Percentage CPU"]["data"][0]["average"], 10.0)

    def test_11_network_metrics_each_type(self) -> None:
        expected = {
            "nsg": ("Microsoft.Network/networkSecurityGroups", "PacketCount,ByteCount"),
            "loadbalancer": (
                "Microsoft.Network/loadBalancers",
                "VIPAvailability,DataPathAvailability,ByteCount,PacketCount,SNATConnectionCount",
            ),
            "applicationgateway": (
                "Microsoft.Network/applicationGateways",
                "Throughput,UnhealthyHostCount,ResponseStatus,CurrentConnections",
            ),
            "vnetgateway": (
                "Microsoft.Network/virtualNetworkGateways",
                "AverageBandwidth,P2SBandwidth,TunnelAverageBandwidth",
            ),
            "publicip": (
                "Microsoft.Network/publicIPAddresses",
                "BytesInDDoS,BytesOutDDoS,DDoSTriggerTCPPackets,DDoSTriggerUDPPackets",
            ),
            "firewall": ("Microsoft.Network/azureFirewalls", "DataProcessed,Throughput"),
            "frontdoor": (
                "Microsoft.Network/frontDoors",
                "RequestCount,Latency,BackendHealthPercentage",
            ),
            "cdn": ("Microsoft.Cdn/profiles", "RequestCount,ResponseSize,OriginHealthPercentage"),
        }
        for resource_type, (provider, metrics) in expected.items():
            with self.subTest(resource_type=resource_type):
                self.sdk.results["monitor.metrics.list"] = fake_metrics(points=1)
                payload = self.run_tool(
                    "get_network_metrics",
                    {
                        "resource_group_name": "rg1",
                        "resource_name": "res1",
                        "resource_type": resource_type,
                    },
                )
                kwargs = self.sdk.last_call("monitor.metrics.list")["kwargs"]
                self.assertEqual(
                    kwargs["resource_uri"],
                    f"/subscriptions/{self.subscription_id}/resourceGroups/rg1/providers/{provider}/res1",
                )
                self.assertEqual(kwargs["metricnames"], metrics)
                self.assert_metric_shape(payload)

    def test_12_app_insights_metrics(self) -> None:
        self.sdk.results["monitor.metrics.list"] = fake_metrics("requests/count", "Count")
        payload = self.run_tool(
            "get_app_insights_metrics",
            {"resource_group_name": "rg1", "app_insights_name": "ai1"},
        )
        kwargs = self.sdk.last_call("monitor.metrics.list")["kwargs"]
        self.assertEqual(kwargs["metricnames"], azure_tools.DEFAULT_APP_INSIGHTS_METRICS)
        self.assertTrue(kwargs["resource_uri"].endswith("/Microsoft.Insights/components/ai1"))
        self.assert_metric_shape(payload)

    def test_13_generic_resource_metrics(self) -> None:
        self.sdk.results["monitor.metrics.list"] = fake_metrics()
        payload = self.run_tool(
            "get_resource_metrics",
            {
                "resource_provider": "Microsoft.Compute/virtualMachines",
                "resource_group_name": "rg1",
                "resource_name": "vm1",
                "metric_names": "Percentage CPU",
                "timespan": "PT6H",
            },
        )
        kwargs = self.sdk.last_call("monitor.metrics.list")["kwargs"]
        self.assertEqual(
            kwargs["resource_uri"],
            f"/subscriptions/{self.subscription_id}/resourceGroups/rg1"
            "/providers/Microsoft.Compute/virtualMachines/vm1",
        )
        self.assertEqual(kwargs["metricnames"], "Percentage CPU")
        self.assertEqual(kwargs["timespan"], "PT6H")
        self.assert_metric_shape(payload)

    def test_14_metric_definitions(self) -> None:
        self.sdk.results["monitor.metric_definitions.list"] = [
            obj(name=obj(value="Percentage CPU"), unit="Percent",
                primary_aggregation_type="Average",
                supported_aggregation_types=["Average", "Maximum"]),
            obj(name=obj(value="Network In"), unit="Bytes",
                primary_aggregation_type="Total", supported_aggregation_types=["Total"]),
        ]
        payload = self.run_tool(
            "get_resource_metric_definitions",
            {
                "resource_provider": "Microsoft.Compute/virtualMachines",
                "resource_group_name": "rg1",
                "resource_name": "vm1",
            },
        )
        self.assertEqual(payload["count"], 2)
        self.assertEqual(payload["definitions"][0]["name"], "Percentage CPU")
        self.assertEqual(payload["definitions"][0]["supported_aggregation_types"], ["Average", "Maximum"])


# ---------------------------------------------------------------- 通用行为


class BehaviourTests(AzureToolTestBase):
    REQUIRED_ARGS = {
        "list_vnets_in_resource_group": ["resource_group_name"],
        "get_vnet_details": ["resource_group_name", "vnet_name"],
        "check_subnets_without_nsg": ["resource_group_name"],
        "get_subnet_info": ["resource_group_name", "vnet_name", "subnet_name"],
        "list_nsgs_in_resource_group": ["resource_group_name"],
        "list_storage_accounts": ["resource_group_name"],
        "list_web_apps": ["resource_group_name"],
        "list_sql_servers": ["resource_group_name"],
        "list_keyvaults": ["resource_group_name"],
        "list_aks_clusters": ["resource_group_name"],
        "list_container_instances": ["resource_group_name"],
        "query_resources": ["kql_query"],
        "get_webapp_metrics": ["resource_group_name", "webapp_name"],
        "get_network_metrics": ["resource_group_name", "resource_name", "resource_type"],
        "get_app_insights_metrics": ["resource_group_name", "app_insights_name"],
        "get_resource_metrics": ["resource_provider", "resource_group_name", "resource_name"],
        "get_resource_metric_definitions": ["resource_provider", "resource_group_name", "resource_name"],
    }

    def test_15_required_arguments(self) -> None:
        self.assertEqual(len(self.REQUIRED_ARGS), 17)
        for tool_name, required in self.REQUIRED_ARGS.items():
            with self.subTest(tool=tool_name):
                self.assertIn(required[0], self.expect_error(tool_name, {}))

    def test_16_large_result_is_truncated(self) -> None:
        # 4000 个存储账户 → JSON 远超 100KB
        self.sdk.results["storage.storage_accounts.list_by_resource_group"] = [
            obj(name=f"storageaccount-{i:05d}-" + "x" * 40) for i in range(4000)
        ]
        raw = azure_tools.execute("list_storage_accounts", {"resource_group_name": "rg1"})
        self.assertIn("已截断", raw)
        self.assertLessEqual(len(raw.encode("utf-8")), azure_tools.MAX_RESULT_BYTES + 400)

    def test_17_schemas_are_complete(self) -> None:
        names = [schema["function"]["name"] for schema in azure_tools.SCHEMAS]
        self.assertEqual(len(names), 21)
        self.assertEqual(len(set(names)), 21, "工具名不能重复")

        for schema in azure_tools.SCHEMAS:
            with self.subTest(tool=schema["function"]["name"]):
                function = schema["function"]
                self.assertEqual(schema["type"], "function")
                self.assertTrue(function.get("description"))
                parameters = function["parameters"]
                self.assertEqual(parameters["type"], "object")
                self.assertIn("properties", parameters)
                self.assertIsInstance(parameters.get("required", []), list)
                for prop_name, spec in parameters["properties"].items():
                    self.assertIn("description", spec, f"{prop_name} 缺 description")
                    self.assertIn("type", spec, f"{prop_name} 缺 type")

    def test_18_default_schemas_and_health(self) -> None:
        collected = {schema["function"]["name"] for schema in tools.default_schemas()}
        self.assertTrue(set(azure_tools.TOOL_NAMES) <= collected)
        # azure 21 + meraki 45 + nexus dashboard 31 + ai 1
        self.assertEqual(len(tools.default_schemas()), 98)
        self.assertEqual(sorted(tools.health()["tools"]), sorted(collected))
        self.assertEqual(len(azure_tools.health()["tools"]), 21)


if __name__ == "__main__":
    unittest.main(verbosity=2)
