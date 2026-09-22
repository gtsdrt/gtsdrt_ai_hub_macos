"""
Meraki 工具集（45 个只读查询工具，从 function_app.py 机械迁移）。

认证：只读环境变量 MERAKI_API_KEY，用 httpx 直接打 Dashboard REST API，不引入 Meraki SDK。
默认认证头是 X-Cisco-Meraki-API-Key；用 OAuth token 时把 MERAKI_AUTH_HEADER 设为 bearer。

SCHEMAS / ENDPOINTS / TOOL_SPECS 由 function_app.py 逐条搬运，改需求请同步这三张表。
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Optional

import httpx

logger = logging.getLogger("tools.meraki")

API_KEY_ENV = "MERAKI_API_KEY"
DEFAULT_BASE_URL = "https://api.meraki.com/api/v1"

# ------------------------------------------------------------------ JSON schema（45）

SCHEMAS: list[dict] = [
    {'type': 'function',
     'function': {'name': 'meraki_list_organizations',
                  'description': '列出 Cisco Meraki Dashboard 中所有可访问的组织（Organization）列表',
                  'parameters': {'type': 'object', 'properties': {}, 'required': []}}},
    {'type': 'function',
     'function': {'name': 'meraki_list_networks',
                  'description': '列出指定 Meraki 组织下的所有网络（Network）',
                  'parameters': {'type': 'object',
                                 'properties': {'org_id': {'type': 'string', 'description': 'Meraki 组织 ID'}},
                                 'required': ['org_id']}}},
    {'type': 'function',
     'function': {'name': 'meraki_list_devices',
                  'description': '列出指定 Meraki 网络下的所有设备（包括 vMX、MX、AP、Switch 等）',
                  'parameters': {'type': 'object',
                                 'properties': {'network_id': {'type': 'string',
                                                               'description': 'Meraki 网络 ID，格式如 N_1234567890'}},
                                 'required': ['network_id']}}},
    {'type': 'function',
     'function': {'name': 'meraki_get_vpn_status',
                  'description': '获取指定 Meraki 网络的 VPN 状态（适用于 vMX/MX 网络）',
                  'parameters': {'type': 'object',
                                 'properties': {'network_id': {'type': 'string', 'description': 'Meraki 网络 ID'}},
                                 'required': ['network_id']}}},
    {'type': 'function',
     'function': {'name': 'meraki_get_device_uplink',
                  'description': '获取指定 Meraki 设备的上行链路状态',
                  'parameters': {'type': 'object',
                                 'properties': {'serial': {'type': 'string',
                                                           'description': '设备序列号，如 QBSB-VQ3J-XZ54'}},
                                 'required': ['serial']}}},
    {'type': 'function',
     'function': {'name': 'meraki_get_vmx_licenses',
                  'description': '获取指定组织中所有 vMX 相关的许可证信息',
                  'parameters': {'type': 'object',
                                 'properties': {'org_id': {'type': 'string', 'description': 'Meraki 组织 ID'}},
                                 'required': ['org_id']}}},
    {'type': 'function',
     'function': {'name': 'meraki_get_network_overview',
                  'description': '获取 Meraki 网络全景信息：网络详情 + 设备列表 + VPN 状态（一站式查询）',
                  'parameters': {'type': 'object',
                                 'properties': {'org_id': {'type': 'string', 'description': 'Meraki 组织 ID'},
                                                'network_id': {'type': 'string', 'description': 'Meraki 网络 ID'}},
                                 'required': ['org_id', 'network_id']}}},
    {'type': 'function',
     'function': {'name': 'meraki_get_appliance_settings',
                  'description': '获取指定 Meraki 网络（MX/Z）的 Appliance 设置',
                  'parameters': {'type': 'object',
                                 'properties': {'network_id': {'type': 'string', 'description': 'Meraki 网络 ID'}},
                                 'required': ['network_id']}}},
    {'type': 'function',
     'function': {'name': 'meraki_get_appliance_vlans',
                  'description': '列出指定 Meraki 网络（MX/Z）的所有 VLAN',
                  'parameters': {'type': 'object',
                                 'properties': {'network_id': {'type': 'string', 'description': 'Meraki 网络 ID'}},
                                 'required': ['network_id']}}},
    {'type': 'function',
     'function': {'name': 'meraki_get_appliance_vlan',
                  'description': '获取指定 Meraki 网络的某个 VLAN 详情',
                  'parameters': {'type': 'object',
                                 'properties': {'network_id': {'type': 'string', 'description': 'Meraki 网络 ID'},
                                                'vlan_id': {'type': 'string', 'description': 'VLAN ID'}},
                                 'required': ['network_id', 'vlan_id']}}},
    {'type': 'function',
     'function': {'name': 'meraki_get_appliance_ports',
                  'description': '列出 MX 安全设备所有端口的 VLAN 设置',
                  'parameters': {'type': 'object',
                                 'properties': {'network_id': {'type': 'string', 'description': 'Meraki 网络 ID'}},
                                 'required': ['network_id']}}},
    {'type': 'function',
     'function': {'name': 'meraki_get_appliance_port',
                  'description': '获取 MX 安全设备单个端口的 VLAN 设置',
                  'parameters': {'type': 'object',
                                 'properties': {'network_id': {'type': 'string', 'description': 'Meraki 网络 ID'},
                                                'port_id': {'type': 'string', 'description': '端口 ID'}},
                                 'required': ['network_id', 'port_id']}}},
    {'type': 'function',
     'function': {'name': 'meraki_get_appliance_static_routes',
                  'description': '列出 MX 网络的静态路由',
                  'parameters': {'type': 'object',
                                 'properties': {'network_id': {'type': 'string', 'description': 'Meraki 网络 ID'}},
                                 'required': ['network_id']}}},
    {'type': 'function',
     'function': {'name': 'meraki_get_appliance_single_lan',
                  'description': '获取 MX 网络的 Single LAN 配置',
                  'parameters': {'type': 'object',
                                 'properties': {'network_id': {'type': 'string', 'description': 'Meraki 网络 ID'}},
                                 'required': ['network_id']}}},
    {'type': 'function',
     'function': {'name': 'meraki_get_firewall_l3_rules',
                  'description': '获取 MX 网络的 L3 防火墙规则',
                  'parameters': {'type': 'object',
                                 'properties': {'network_id': {'type': 'string', 'description': 'Meraki 网络 ID'}},
                                 'required': ['network_id']}}},
    {'type': 'function',
     'function': {'name': 'meraki_get_firewall_l7_rules',
                  'description': '获取 MX 网络的 L7 防火墙规则',
                  'parameters': {'type': 'object',
                                 'properties': {'network_id': {'type': 'string', 'description': 'Meraki 网络 ID'}},
                                 'required': ['network_id']}}},
    {'type': 'function',
     'function': {'name': 'meraki_get_firewall_1to1_nat',
                  'description': '获取 MX 网络的 1:1 NAT 规则',
                  'parameters': {'type': 'object',
                                 'properties': {'network_id': {'type': 'string', 'description': 'Meraki 网络 ID'}},
                                 'required': ['network_id']}}},
    {'type': 'function',
     'function': {'name': 'meraki_get_firewall_1toMany_nat',
                  'description': '获取 MX 网络的 1:Many NAT 规则',
                  'parameters': {'type': 'object',
                                 'properties': {'network_id': {'type': 'string', 'description': 'Meraki 网络 ID'}},
                                 'required': ['network_id']}}},
    {'type': 'function',
     'function': {'name': 'meraki_get_firewall_port_forwarding',
                  'description': '获取 MX 网络的端口转发规则',
                  'parameters': {'type': 'object',
                                 'properties': {'network_id': {'type': 'string', 'description': 'Meraki 网络 ID'}},
                                 'required': ['network_id']}}},
    {'type': 'function',
     'function': {'name': 'meraki_get_security_intrusion',
                  'description': '获取 MX 网络的入侵防护设置',
                  'parameters': {'type': 'object',
                                 'properties': {'network_id': {'type': 'string', 'description': 'Meraki 网络 ID'}},
                                 'required': ['network_id']}}},
    {'type': 'function',
     'function': {'name': 'meraki_get_security_malware',
                  'description': '获取 MX 网络的恶意软件防护设置',
                  'parameters': {'type': 'object',
                                 'properties': {'network_id': {'type': 'string', 'description': 'Meraki 网络 ID'}},
                                 'required': ['network_id']}}},
    {'type': 'function',
     'function': {'name': 'meraki_get_content_filtering',
                  'description': '获取 MX 网络的内容过滤设置',
                  'parameters': {'type': 'object',
                                 'properties': {'network_id': {'type': 'string', 'description': 'Meraki 网络 ID'}},
                                 'required': ['network_id']}}},
    {'type': 'function',
     'function': {'name': 'meraki_get_vpn_site_to_site',
                  'description': '获取 MX 网络的 Site-to-Site VPN 设置',
                  'parameters': {'type': 'object',
                                 'properties': {'network_id': {'type': 'string', 'description': 'Meraki 网络 ID'}},
                                 'required': ['network_id']}}},
    {'type': 'function',
     'function': {'name': 'meraki_get_vpn_bgp',
                  'description': '获取 MX 网络的 VPN BGP 配置',
                  'parameters': {'type': 'object',
                                 'properties': {'network_id': {'type': 'string', 'description': 'Meraki 网络 ID'}},
                                 'required': ['network_id']}}},
    {'type': 'function',
     'function': {'name': 'meraki_get_traffic_shaping',
                  'description': '获取 MX 网络的流量整形设置',
                  'parameters': {'type': 'object',
                                 'properties': {'network_id': {'type': 'string', 'description': 'Meraki 网络 ID'}},
                                 'required': ['network_id']}}},
    {'type': 'function',
     'function': {'name': 'meraki_get_traffic_shaping_rules',
                  'description': '获取 MX 网络的流量整形规则',
                  'parameters': {'type': 'object',
                                 'properties': {'network_id': {'type': 'string', 'description': 'Meraki 网络 ID'}},
                                 'required': ['network_id']}}},
    {'type': 'function',
     'function': {'name': 'meraki_get_traffic_shaping_uplink_bandwidth',
                  'description': '获取 MX 网络的上行带宽设置',
                  'parameters': {'type': 'object',
                                 'properties': {'network_id': {'type': 'string', 'description': 'Meraki 网络 ID'}},
                                 'required': ['network_id']}}},
    {'type': 'function',
     'function': {'name': 'meraki_get_traffic_shaping_uplink_selection',
                  'description': '获取 MX 网络的上行选择（SD-WAN）设置',
                  'parameters': {'type': 'object',
                                 'properties': {'network_id': {'type': 'string', 'description': 'Meraki 网络 ID'}},
                                 'required': ['network_id']}}},
    {'type': 'function',
     'function': {'name': 'meraki_get_device_appliance_uplinks_settings',
                  'description': '获取指定 MX 设备的上行链路设置',
                  'parameters': {'type': 'object',
                                 'properties': {'serial': {'type': 'string', 'description': '设备序列号'}},
                                 'required': ['serial']}}},
    {'type': 'function',
     'function': {'name': 'meraki_get_org_appliance_uplink_statuses',
                  'description': '列出组织中所有 MX/Z 设备的上行状态',
                  'parameters': {'type': 'object',
                                 'properties': {'org_id': {'type': 'string', 'description': 'Meraki 组织 ID'}},
                                 'required': ['org_id']}}},
    {'type': 'function',
     'function': {'name': 'meraki_get_warm_spare',
                  'description': '获取 MX 网络的 Warm Spare（高可用）设置',
                  'parameters': {'type': 'object',
                                 'properties': {'network_id': {'type': 'string', 'description': 'Meraki 网络 ID'}},
                                 'required': ['network_id']}}},
    {'type': 'function',
     'function': {'name': 'meraki_get_appliance_ssids',
                  'description': '列出 MX 网络的所有 SSID（MX 内置无线功能）',
                  'parameters': {'type': 'object',
                                 'properties': {'network_id': {'type': 'string', 'description': 'Meraki 网络 ID'}},
                                 'required': ['network_id']}}},
    {'type': 'function',
     'function': {'name': 'meraki_get_appliance_ssid',
                  'description': '获取 MX 网络的某个 SSID 详情',
                  'parameters': {'type': 'object',
                                 'properties': {'network_id': {'type': 'string', 'description': 'Meraki 网络 ID'},
                                                'ssid_number': {'type': 'string',
                                                                'description': 'SSID 编号，如 0, 1, 2...'}},
                                 'required': ['network_id', 'ssid_number']}}},
    {'type': 'function',
     'function': {'name': 'meraki_get_appliance_rf_profiles',
                  'description': '列出 MX 网络的所有 RF 配置文件',
                  'parameters': {'type': 'object',
                                 'properties': {'network_id': {'type': 'string', 'description': 'Meraki 网络 ID'}},
                                 'required': ['network_id']}}},
    {'type': 'function',
     'function': {'name': 'meraki_get_org_appliance_vlans',
                  'description': '列出组织中所有 MX 网络的 VLAN（组织级视图）',
                  'parameters': {'type': 'object',
                                 'properties': {'org_id': {'type': 'string', 'description': 'Meraki 组织 ID'}},
                                 'required': ['org_id']}}},
    {'type': 'function',
     'function': {'name': 'meraki_get_device_switch_ports',
                  'description': '列出某台 Meraki 交换机的所有端口配置',
                  'parameters': {'type': 'object',
                                 'properties': {'serial': {'type': 'string',
                                                           'description': '交换机设备序列号，如 QBSB-VQ3J-XZ54'}},
                                 'required': ['serial']}}},
    {'type': 'function',
     'function': {'name': 'meraki_get_device_switch_port',
                  'description': '返回某台 Meraki 交换机的单个端口配置',
                  'parameters': {'type': 'object',
                                 'properties': {'serial': {'type': 'string', 'description': '交换机设备序列号'},
                                                'port_id': {'type': 'string',
                                                            'description': '端口 ID，如 1, 2, 3...'}},
                                 'required': ['serial', 'port_id']}}},
    {'type': 'function',
     'function': {'name': 'meraki_get_device_switch_ports_statuses',
                  'description': '返回某台 Meraki 交换机所有端口的实时状态',
                  'parameters': {'type': 'object',
                                 'properties': {'serial': {'type': 'string', 'description': '交换机设备序列号'}},
                                 'required': ['serial']}}},
    {'type': 'function',
     'function': {'name': 'meraki_get_device_switch_ports_statuses_packets',
                  'description': '返回某台 Meraki 交换机所有端口的包计数器',
                  'parameters': {'type': 'object',
                                 'properties': {'serial': {'type': 'string', 'description': '交换机设备序列号'}},
                                 'required': ['serial']}}},
    {'type': 'function',
     'function': {'name': 'meraki_get_org_switch_ports_by_switch',
                  'description': '按交换机列出组织内所有端口配置',
                  'parameters': {'type': 'object',
                                 'properties': {'org_id': {'type': 'string', 'description': 'Meraki 组织 ID'}},
                                 'required': ['org_id']}}},
    {'type': 'function',
     'function': {'name': 'meraki_get_org_switch_ports_statuses_by_switch',
                  'description': '按交换机列出组织内所有端口状态',
                  'parameters': {'type': 'object',
                                 'properties': {'org_id': {'type': 'string', 'description': 'Meraki 组织 ID'}},
                                 'required': ['org_id']}}},
    {'type': 'function',
     'function': {'name': 'meraki_get_org_switch_ports_statuses_packets_by_device_by_port',
                  'description': '按设备和端口列出组织内所有端口的包计数器',
                  'parameters': {'type': 'object',
                                 'properties': {'org_id': {'type': 'string', 'description': 'Meraki 组织 ID'}},
                                 'required': ['org_id']}}},
    {'type': 'function',
     'function': {'name': 'meraki_get_org_switch_ports_mirrors_by_switch',
                  'description': '按交换机列出组织内的端口镜像配置',
                  'parameters': {'type': 'object',
                                 'properties': {'org_id': {'type': 'string', 'description': 'Meraki 组织 ID'}},
                                 'required': ['org_id']}}},
    {'type': 'function',
     'function': {'name': 'meraki_get_org_switch_stacks_ports_mirrors_by_stack',
                  'description': '按堆叠列出组织内的端口镜像配置',
                  'parameters': {'type': 'object',
                                 'properties': {'org_id': {'type': 'string', 'description': 'Meraki 组织 ID'}},
                                 'required': ['org_id']}}},
    {'type': 'function',
     'function': {'name': 'meraki_get_org_config_templates_switch_profiles_ports_mirrors_by_switch_profile',
                  'description': '按模板配置文件列出端口镜像配置',
                  'parameters': {'type': 'object',
                                 'properties': {'org_id': {'type': 'string', 'description': 'Meraki 组织 ID'},
                                                'config_template_id': {'type': 'string',
                                                                       'description': '配置模板 ID'},
                                                'profile_id': {'type': 'string',
                                                               'description': 'Switch Profile ID'}},
                                 'required': ['org_id', 'config_template_id', 'profile_id']}}},
]

TOOL_NAMES: tuple[str, ...] = tuple(schema["function"]["name"] for schema in SCHEMAS)

# ------------------------------------------------------------------ 端点与方法映射

# client 方法名 → (HTTP 方法, 路径模板)；模板里的 {arg} 用工具参数填充
ENDPOINTS: dict[str, tuple[str, str]] = {
    "get_device": ('GET', '/devices/{serial}'),
    "get_device_appliance_uplinks_settings": ('GET', '/devices/{serial}/appliance/uplinks/settings'),
    "get_device_switch_port": ('GET', '/devices/{serial}/switch/ports/{port_id}'),
    "get_device_switch_ports": ('GET', '/devices/{serial}/switch/ports'),
    "get_device_switch_ports_statuses": ('GET', '/devices/{serial}/switch/ports/statuses'),
    "get_device_switch_ports_statuses_packets": ('GET', '/devices/{serial}/switch/ports/statuses/packets'),
    "get_network_appliance_content_filtering": ('GET', '/networks/{network_id}/appliance/contentFiltering'),
    "get_network_appliance_firewall_l3_rules": ('GET', '/networks/{network_id}/appliance/firewall/l3FirewallRules'),
    "get_network_appliance_firewall_l7_rules": ('GET', '/networks/{network_id}/appliance/firewall/l7FirewallRules'),
    "get_network_appliance_firewall_one_to_many_nat": ('GET', '/networks/{network_id}/appliance/firewall/oneToManyNatRules'),
    "get_network_appliance_firewall_one_to_one_nat": ('GET', '/networks/{network_id}/appliance/firewall/oneToOneNatRules'),
    "get_network_appliance_firewall_port_forwarding": ('GET', '/networks/{network_id}/appliance/firewall/portForwardingRules'),
    "get_network_appliance_port": ('GET', '/networks/{network_id}/appliance/ports/{port_id}'),
    "get_network_appliance_ports": ('GET', '/networks/{network_id}/appliance/ports'),
    "get_network_appliance_rf_profiles": ('GET', '/networks/{network_id}/appliance/rfProfiles'),
    "get_network_appliance_security_intrusion": ('GET', '/networks/{network_id}/appliance/security/intrusion'),
    "get_network_appliance_security_malware": ('GET', '/networks/{network_id}/appliance/security/malware'),
    "get_network_appliance_settings": ('GET', '/networks/{network_id}/appliance/settings'),
    "get_network_appliance_single_lan": ('GET', '/networks/{network_id}/appliance/singleLan'),
    "get_network_appliance_ssid": ('GET', '/networks/{network_id}/appliance/ssids/{ssid_number}'),
    "get_network_appliance_ssids": ('GET', '/networks/{network_id}/appliance/ssids'),
    "get_network_appliance_static_routes": ('GET', '/networks/{network_id}/appliance/staticRoutes'),
    "get_network_appliance_traffic_shaping": ('GET', '/networks/{network_id}/appliance/trafficShaping'),
    "get_network_appliance_traffic_shaping_rules": ('GET', '/networks/{network_id}/appliance/trafficShaping/rules'),
    "get_network_appliance_traffic_shaping_uplink_bandwidth": ('GET', '/networks/{network_id}/appliance/trafficShaping/uplinkBandwidth'),
    "get_network_appliance_traffic_shaping_uplink_selection": ('GET', '/networks/{network_id}/appliance/trafficShaping/uplinkSelection'),
    "get_network_appliance_vlan": ('GET', '/networks/{network_id}/appliance/vlans/{vlan_id}'),
    "get_network_appliance_vlans": ('GET', '/networks/{network_id}/appliance/vlans'),
    "get_network_appliance_vpn_bgp": ('GET', '/networks/{network_id}/appliance/vpn/bgp'),
    "get_network_appliance_vpn_site_to_site": ('GET', '/networks/{network_id}/appliance/vpn/siteToSiteVpn'),
    "get_network_appliance_warm_spare": ('GET', '/networks/{network_id}/appliance/warmSpare'),
    "get_network_devices": ('GET', '/networks/{network_id}/devices'),
    "get_networks": ('GET', '/organizations/{org_id}/networks'),
    "get_organization": ('GET', '/organizations/{org_id}'),
    "get_organization_appliance_uplink_statuses": ('GET', '/organizations/{org_id}/appliance/uplink/statuses'),
    "get_organization_appliance_vlans": ('GET', '/organizations/{org_id}/appliance/vlans'),
    "get_organization_config_templates_switch_profiles_ports_mirrors_by_switch_profile": ('GET', '/organizations/{org_id}/configTemplates/{config_template_id}/switch/profiles/{profile_id}/ports/mirrors/bySwitch'),
    "get_organization_switch_ports_by_switch": ('GET', '/organizations/{org_id}/switch/ports/bySwitch'),
    "get_organization_switch_ports_mirrors_by_switch": ('GET', '/organizations/{org_id}/switch/ports/mirrors/bySwitch'),
    "get_organization_switch_ports_statuses_by_switch": ('GET', '/organizations/{org_id}/switch/ports/statuses/bySwitch'),
    "get_organization_switch_ports_statuses_packets_by_device_by_port": ('GET', '/organizations/{org_id}/switch/ports/statuses/packets/byDevice/byPort'),
    "get_organization_switch_stacks_ports_mirrors_by_stack": ('GET', '/organizations/{org_id}/switch/stacks/ports/mirrors/byStack'),
    "get_organizations": ('GET', '/organizations'),
    "get_uplink_status": ('GET', '/devices/{serial}/uplinks'),
    "get_vmx_licenses": ('GET', '/organizations/{org_id}/licenses'),
    "get_vpn_status": ('GET', '/networks/{network_id}/appliance/vpn/status'),
}

# 工具名 → (action, client 方法, 参数顺序, 回显字段)
TOOL_SPECS: dict[str, dict] = {
    "meraki_list_organizations": {"action": 'list_organizations', "method": 'get_organizations', "params": (), "echo": ()},
    "meraki_list_networks": {"action": 'list_networks', "method": 'get_networks', "params": ('org_id',), "echo": ('org_id',)},
    "meraki_list_devices": {"action": 'list_devices', "method": 'get_network_devices', "params": ('network_id',), "echo": ('network_id',)},
    "meraki_get_vpn_status": {"action": 'get_vpn_status', "method": 'get_vpn_status', "params": ('network_id',), "echo": ('network_id',)},
    "meraki_get_device_uplink": {"action": 'get_uplink_status', "method": 'get_uplink_status', "params": ('serial',), "echo": ('serial',)},
    "meraki_get_vmx_licenses": {"action": 'get_vmx_licenses', "method": 'get_vmx_licenses', "params": ('org_id',), "echo": ('org_id',)},
    "meraki_get_network_overview": {"action": 'get_network_overview', "method": 'get_networks', "params": ('org_id',), "echo": ('org_id', 'network_id')},
    "meraki_get_appliance_settings": {"action": 'get_appliance_settings', "method": 'get_network_appliance_settings', "params": ('network_id',), "echo": ('network_id',)},
    "meraki_get_appliance_vlans": {"action": 'get_appliance_vlans', "method": 'get_network_appliance_vlans', "params": ('network_id',), "echo": ('network_id',)},
    "meraki_get_appliance_vlan": {"action": 'get_appliance_vlan', "method": 'get_network_appliance_vlan', "params": ('network_id', 'vlan_id'), "echo": ('network_id', 'vlan_id')},
    "meraki_get_appliance_ports": {"action": 'get_appliance_ports', "method": 'get_network_appliance_ports', "params": ('network_id',), "echo": ('network_id',)},
    "meraki_get_appliance_port": {"action": 'get_appliance_port', "method": 'get_network_appliance_port', "params": ('network_id', 'port_id'), "echo": ('network_id', 'port_id')},
    "meraki_get_appliance_static_routes": {"action": 'get_appliance_static_routes', "method": 'get_network_appliance_static_routes', "params": ('network_id',), "echo": ('network_id',)},
    "meraki_get_appliance_single_lan": {"action": 'get_appliance_single_lan', "method": 'get_network_appliance_single_lan', "params": ('network_id',), "echo": ('network_id',)},
    "meraki_get_firewall_l3_rules": {"action": 'get_firewall_l3_rules', "method": 'get_network_appliance_firewall_l3_rules', "params": ('network_id',), "echo": ('network_id',)},
    "meraki_get_firewall_l7_rules": {"action": 'get_firewall_l7_rules', "method": 'get_network_appliance_firewall_l7_rules', "params": ('network_id',), "echo": ('network_id',)},
    "meraki_get_firewall_1to1_nat": {"action": 'get_firewall_1to1_nat', "method": 'get_network_appliance_firewall_one_to_one_nat', "params": ('network_id',), "echo": ('network_id',)},
    "meraki_get_firewall_1toMany_nat": {"action": 'get_firewall_1toMany_nat', "method": 'get_network_appliance_firewall_one_to_many_nat', "params": ('network_id',), "echo": ('network_id',)},
    "meraki_get_firewall_port_forwarding": {"action": 'get_firewall_port_forwarding', "method": 'get_network_appliance_firewall_port_forwarding', "params": ('network_id',), "echo": ('network_id',)},
    "meraki_get_security_intrusion": {"action": 'get_security_intrusion', "method": 'get_network_appliance_security_intrusion', "params": ('network_id',), "echo": ('network_id',)},
    "meraki_get_security_malware": {"action": 'get_security_malware', "method": 'get_network_appliance_security_malware', "params": ('network_id',), "echo": ('network_id',)},
    "meraki_get_content_filtering": {"action": 'get_content_filtering', "method": 'get_network_appliance_content_filtering', "params": ('network_id',), "echo": ('network_id',)},
    "meraki_get_vpn_site_to_site": {"action": 'get_vpn_site_to_site', "method": 'get_network_appliance_vpn_site_to_site', "params": ('network_id',), "echo": ('network_id',)},
    "meraki_get_vpn_bgp": {"action": 'get_vpn_bgp', "method": 'get_network_appliance_vpn_bgp', "params": ('network_id',), "echo": ('network_id',)},
    "meraki_get_traffic_shaping": {"action": 'get_traffic_shaping', "method": 'get_network_appliance_traffic_shaping', "params": ('network_id',), "echo": ('network_id',)},
    "meraki_get_traffic_shaping_rules": {"action": 'get_traffic_shaping_rules', "method": 'get_network_appliance_traffic_shaping_rules', "params": ('network_id',), "echo": ('network_id',)},
    "meraki_get_traffic_shaping_uplink_bandwidth": {"action": 'get_traffic_shaping_uplink_bandwidth', "method": 'get_network_appliance_traffic_shaping_uplink_bandwidth', "params": ('network_id',), "echo": ('network_id',)},
    "meraki_get_traffic_shaping_uplink_selection": {"action": 'get_traffic_shaping_uplink_selection', "method": 'get_network_appliance_traffic_shaping_uplink_selection', "params": ('network_id',), "echo": ('network_id',)},
    "meraki_get_device_appliance_uplinks_settings": {"action": 'get_device_appliance_uplinks_settings', "method": 'get_device_appliance_uplinks_settings', "params": ('serial',), "echo": ('serial',)},
    "meraki_get_org_appliance_uplink_statuses": {"action": 'get_org_appliance_uplink_statuses', "method": 'get_organization_appliance_uplink_statuses', "params": ('org_id',), "echo": ('org_id',)},
    "meraki_get_warm_spare": {"action": 'get_warm_spare', "method": 'get_network_appliance_warm_spare', "params": ('network_id',), "echo": ('network_id',)},
    "meraki_get_appliance_ssids": {"action": 'get_appliance_ssids', "method": 'get_network_appliance_ssids', "params": ('network_id',), "echo": ('network_id',)},
    "meraki_get_appliance_ssid": {"action": 'get_appliance_ssid', "method": 'get_network_appliance_ssid', "params": ('network_id', 'ssid_number'), "echo": ('network_id', 'ssid_number')},
    "meraki_get_appliance_rf_profiles": {"action": 'get_appliance_rf_profiles', "method": 'get_network_appliance_rf_profiles', "params": ('network_id',), "echo": ('network_id',)},
    "meraki_get_org_appliance_vlans": {"action": 'get_org_appliance_vlans', "method": 'get_organization_appliance_vlans', "params": ('org_id',), "echo": ('org_id',)},
    "meraki_get_device_switch_ports": {"action": 'get_device_switch_ports', "method": 'get_device_switch_ports', "params": ('serial',), "echo": ('serial',)},
    "meraki_get_device_switch_port": {"action": 'get_device_switch_port', "method": 'get_device_switch_port', "params": ('serial', 'port_id'), "echo": ('serial', 'port_id')},
    "meraki_get_device_switch_ports_statuses": {"action": 'get_device_switch_ports_statuses', "method": 'get_device_switch_ports_statuses', "params": ('serial',), "echo": ('serial',)},
    "meraki_get_device_switch_ports_statuses_packets": {"action": 'get_device_switch_ports_statuses_packets', "method": 'get_device_switch_ports_statuses_packets', "params": ('serial',), "echo": ('serial',)},
    "meraki_get_org_switch_ports_by_switch": {"action": 'get_org_switch_ports_by_switch', "method": 'get_organization_switch_ports_by_switch', "params": ('org_id',), "echo": ('org_id',)},
    "meraki_get_org_switch_ports_statuses_by_switch": {"action": 'get_org_switch_ports_statuses_by_switch', "method": 'get_organization_switch_ports_statuses_by_switch', "params": ('org_id',), "echo": ('org_id',)},
    "meraki_get_org_switch_ports_statuses_packets_by_device_by_port": {"action": 'get_org_switch_ports_statuses_packets_by_device_by_port', "method": 'get_organization_switch_ports_statuses_packets_by_device_by_port', "params": ('org_id',), "echo": ('org_id',)},
    "meraki_get_org_switch_ports_mirrors_by_switch": {"action": 'get_org_switch_ports_mirrors_by_switch', "method": 'get_organization_switch_ports_mirrors_by_switch', "params": ('org_id',), "echo": ('org_id',)},
    "meraki_get_org_switch_stacks_ports_mirrors_by_stack": {"action": 'get_org_switch_stacks_ports_mirrors_by_stack', "method": 'get_organization_switch_stacks_ports_mirrors_by_stack', "params": ('org_id',), "echo": ('org_id',)},
    "meraki_get_org_config_templates_switch_profiles_ports_mirrors_by_switch_profile": {"action": 'get_org_config_templates_switch_profiles_ports_mirrors_by_switch_profile', "method": 'get_organization_config_templates_switch_profiles_ports_mirrors_by_switch_profile', "params": ('org_id', 'config_template_id', 'profile_id'), "echo": ('org_id',)},
}

SPECIAL_TOOLS = ("meraki_get_network_overview",)

# ------------------------------------------------------------------ 结果与配置


def _ok(**payload: Any) -> str:
    return json.dumps({"status": "success", "provider": "meraki", **payload}, ensure_ascii=False)


def _error(message: str) -> str:
    return json.dumps({"status": "error", "message": message}, ensure_ascii=False)


def base_url() -> str:
    return (os.environ.get("MERAKI_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")


def api_key() -> str:
    return (os.environ.get(API_KEY_ENV) or "").strip()


def is_configured() -> bool:
    return bool(api_key())


def health() -> dict:
    configured = is_configured()
    return {
        "configured": configured,
        "api_key_env": API_KEY_ENV,
        "base_url": base_url(),
        "tools": list(TOOL_NAMES),
        "hint": None if configured else f"未配置 {API_KEY_ENV}，Meraki 工具不可用",
    }


# ------------------------------------------------------------------ HTTP


def _headers() -> dict:
    key = api_key()
    if not key:
        raise RuntimeError(f"未配置 {API_KEY_ENV}")

    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    if (os.environ.get("MERAKI_AUTH_HEADER") or "").strip().lower() == "bearer":
        headers["Authorization"] = f"Bearer {key}"
    else:
        headers["X-Cisco-Meraki-API-Key"] = key
    return headers


def _request(path: str, params: Optional[dict] = None) -> Any:
    url = f"{base_url()}{path}"
    with httpx.Client(timeout=30.0) as client:
        response = client.get(url, headers=_headers(), params=params)

    if response.status_code >= 400:
        snippet = response.text[:300].replace("\n", " ")
        raise RuntimeError(f"Meraki API {path} 返回 HTTP {response.status_code}：{snippet}")

    if not response.text:
        return {}
    return response.json()


def _require(arguments: dict, name: str) -> str:
    value = arguments.get(name)
    if value is None or str(value).strip() == "":
        raise ValueError(f"{name} 是必填参数")
    return str(value)


def _dispatch(method: str, kwargs: dict) -> Any:
    verb, template = ENDPOINTS[method]
    path = template.format(**kwargs) if kwargs else template
    return _request(path) if verb == "GET" else _request(path)


# ------------------------------------------------------------------ 工具实现


def _run_tool(tool_name: str, arguments: dict) -> str:
    spec = TOOL_SPECS[tool_name]
    kwargs = {name: _require(arguments, name) for name in spec["params"]}
    data = _dispatch(spec["method"], kwargs)
    echo = {name: kwargs[name] for name in spec["echo"] if name in kwargs}
    return _ok(action=spec["action"], data=data, **echo)


def _network_overview(arguments: dict) -> str:
    """原 meraki_get_network_overview 的特殊逻辑：网络 + 设备 + VPN 状态"""
    org_id = _require(arguments, "org_id")
    network_id = _require(arguments, "network_id")

    overview: dict = {"network": None, "devices": [], "vpn_status": None}
    for network in _dispatch("get_networks", {"org_id": org_id}) or []:
        if network.get("id") == network_id:
            overview["network"] = network
            break
    overview["devices"] = _dispatch("get_network_devices", {"network_id": network_id})
    try:
        overview["vpn_status"] = _dispatch("get_vpn_status", {"network_id": network_id})
    except Exception:
        overview["vpn_status"] = {"error": "VPN status not available for this network type"}

    return _ok(action="get_network_overview", org_id=org_id, network_id=network_id, data=overview)


_HANDLERS = {
    "meraki_list_organizations": lambda arguments, _name="meraki_list_organizations": _run_tool(_name, arguments),
    "meraki_list_networks": lambda arguments, _name="meraki_list_networks": _run_tool(_name, arguments),
    "meraki_list_devices": lambda arguments, _name="meraki_list_devices": _run_tool(_name, arguments),
    "meraki_get_vpn_status": lambda arguments, _name="meraki_get_vpn_status": _run_tool(_name, arguments),
    "meraki_get_device_uplink": lambda arguments, _name="meraki_get_device_uplink": _run_tool(_name, arguments),
    "meraki_get_vmx_licenses": lambda arguments, _name="meraki_get_vmx_licenses": _run_tool(_name, arguments),
    "meraki_get_network_overview": _network_overview,
    "meraki_get_appliance_settings": lambda arguments, _name="meraki_get_appliance_settings": _run_tool(_name, arguments),
    "meraki_get_appliance_vlans": lambda arguments, _name="meraki_get_appliance_vlans": _run_tool(_name, arguments),
    "meraki_get_appliance_vlan": lambda arguments, _name="meraki_get_appliance_vlan": _run_tool(_name, arguments),
    "meraki_get_appliance_ports": lambda arguments, _name="meraki_get_appliance_ports": _run_tool(_name, arguments),
    "meraki_get_appliance_port": lambda arguments, _name="meraki_get_appliance_port": _run_tool(_name, arguments),
    "meraki_get_appliance_static_routes": lambda arguments, _name="meraki_get_appliance_static_routes": _run_tool(_name, arguments),
    "meraki_get_appliance_single_lan": lambda arguments, _name="meraki_get_appliance_single_lan": _run_tool(_name, arguments),
    "meraki_get_firewall_l3_rules": lambda arguments, _name="meraki_get_firewall_l3_rules": _run_tool(_name, arguments),
    "meraki_get_firewall_l7_rules": lambda arguments, _name="meraki_get_firewall_l7_rules": _run_tool(_name, arguments),
    "meraki_get_firewall_1to1_nat": lambda arguments, _name="meraki_get_firewall_1to1_nat": _run_tool(_name, arguments),
    "meraki_get_firewall_1toMany_nat": lambda arguments, _name="meraki_get_firewall_1toMany_nat": _run_tool(_name, arguments),
    "meraki_get_firewall_port_forwarding": lambda arguments, _name="meraki_get_firewall_port_forwarding": _run_tool(_name, arguments),
    "meraki_get_security_intrusion": lambda arguments, _name="meraki_get_security_intrusion": _run_tool(_name, arguments),
    "meraki_get_security_malware": lambda arguments, _name="meraki_get_security_malware": _run_tool(_name, arguments),
    "meraki_get_content_filtering": lambda arguments, _name="meraki_get_content_filtering": _run_tool(_name, arguments),
    "meraki_get_vpn_site_to_site": lambda arguments, _name="meraki_get_vpn_site_to_site": _run_tool(_name, arguments),
    "meraki_get_vpn_bgp": lambda arguments, _name="meraki_get_vpn_bgp": _run_tool(_name, arguments),
    "meraki_get_traffic_shaping": lambda arguments, _name="meraki_get_traffic_shaping": _run_tool(_name, arguments),
    "meraki_get_traffic_shaping_rules": lambda arguments, _name="meraki_get_traffic_shaping_rules": _run_tool(_name, arguments),
    "meraki_get_traffic_shaping_uplink_bandwidth": lambda arguments, _name="meraki_get_traffic_shaping_uplink_bandwidth": _run_tool(_name, arguments),
    "meraki_get_traffic_shaping_uplink_selection": lambda arguments, _name="meraki_get_traffic_shaping_uplink_selection": _run_tool(_name, arguments),
    "meraki_get_device_appliance_uplinks_settings": lambda arguments, _name="meraki_get_device_appliance_uplinks_settings": _run_tool(_name, arguments),
    "meraki_get_org_appliance_uplink_statuses": lambda arguments, _name="meraki_get_org_appliance_uplink_statuses": _run_tool(_name, arguments),
    "meraki_get_warm_spare": lambda arguments, _name="meraki_get_warm_spare": _run_tool(_name, arguments),
    "meraki_get_appliance_ssids": lambda arguments, _name="meraki_get_appliance_ssids": _run_tool(_name, arguments),
    "meraki_get_appliance_ssid": lambda arguments, _name="meraki_get_appliance_ssid": _run_tool(_name, arguments),
    "meraki_get_appliance_rf_profiles": lambda arguments, _name="meraki_get_appliance_rf_profiles": _run_tool(_name, arguments),
    "meraki_get_org_appliance_vlans": lambda arguments, _name="meraki_get_org_appliance_vlans": _run_tool(_name, arguments),
    "meraki_get_device_switch_ports": lambda arguments, _name="meraki_get_device_switch_ports": _run_tool(_name, arguments),
    "meraki_get_device_switch_port": lambda arguments, _name="meraki_get_device_switch_port": _run_tool(_name, arguments),
    "meraki_get_device_switch_ports_statuses": lambda arguments, _name="meraki_get_device_switch_ports_statuses": _run_tool(_name, arguments),
    "meraki_get_device_switch_ports_statuses_packets": lambda arguments, _name="meraki_get_device_switch_ports_statuses_packets": _run_tool(_name, arguments),
    "meraki_get_org_switch_ports_by_switch": lambda arguments, _name="meraki_get_org_switch_ports_by_switch": _run_tool(_name, arguments),
    "meraki_get_org_switch_ports_statuses_by_switch": lambda arguments, _name="meraki_get_org_switch_ports_statuses_by_switch": _run_tool(_name, arguments),
    "meraki_get_org_switch_ports_statuses_packets_by_device_by_port": lambda arguments, _name="meraki_get_org_switch_ports_statuses_packets_by_device_by_port": _run_tool(_name, arguments),
    "meraki_get_org_switch_ports_mirrors_by_switch": lambda arguments, _name="meraki_get_org_switch_ports_mirrors_by_switch": _run_tool(_name, arguments),
    "meraki_get_org_switch_stacks_ports_mirrors_by_stack": lambda arguments, _name="meraki_get_org_switch_stacks_ports_mirrors_by_stack": _run_tool(_name, arguments),
    "meraki_get_org_config_templates_switch_profiles_ports_mirrors_by_switch_profile": lambda arguments, _name="meraki_get_org_config_templates_switch_profiles_ports_mirrors_by_switch_profile": _run_tool(_name, arguments),
}


def execute(tool_name: str, arguments: Optional[dict] = None) -> str:
    """执行 Meraki 工具；任何异常都收敛成 {"status": "error", "message": ...}"""
    arguments = arguments or {}
    handler = _HANDLERS.get(tool_name)
    if handler is None:
        return _error(f"未知的 Meraki 工具：{tool_name}")

    try:
        return handler(arguments)
    except Exception as exc:
        logger.warning("[Meraki] %s 执行失败：%s: %s", tool_name, type(exc).__name__, exc)
        return _error(f"{type(exc).__name__}: {exc}")


# ---------------------------------------------------------------- 公开入口
# 连接测试（/api/test_connection）这类场景直接调用，不经过 AI 工具循环。


def meraki_list_organizations(arguments: Optional[dict] = None) -> str:
    return execute("meraki_list_organizations", arguments or {})


def meraki_list_networks(arguments: Optional[dict] = None) -> str:
    return execute("meraki_list_networks", arguments or {})


def meraki_list_devices(arguments: Optional[dict] = None) -> str:
    return execute("meraki_list_devices", arguments or {})

