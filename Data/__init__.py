# ============================================================
# EuoraCraft Launcher Core
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher.Core
#
# 文件作用：Minecraft 与实例本地数据文件的解析、校验与读写原语，不含启动器编排。
#
# 公开接口：
#   - nbt — NBT 二进制格式读写。
#   - instance_health — 实例继承链与组件声明的只读健康检查。
#   - mod_versions — 加载器版本约束判断。
#   - mod_metadata — 本地模组声明与内嵌 Jar 解析。
#   - world_seeds — 世界种子位置识别与多文件原子提交。
#   - instance_options — 实例 options.txt 结构化读写。
#   - resource_files — 本地资源文件的后缀、包元数据与投影结构校验。
# ============================================================
