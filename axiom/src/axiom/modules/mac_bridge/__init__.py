"""AXIOM × macOS 执行桥（Mac Bridge）的设备网关。

职责边界很窄：**只做通道**。设备连进来、指令发出去、结果收回来。
它不做任何决策，也不理解命令的含义——判断什么该做、结果算不算成功，
全在上层的模型里。

设备侧的开关语义也由这里承接：设备主动发 ``bye{reason:operator_off}``
表示操作员关掉了，网关要把状态记成 ``operator_off`` 并**禁止自动重连**，
而不是当成一次普通掉线。
"""

from axiom.modules.mac_bridge.service import MacBridgeService

__all__ = ["MacBridgeService"]
