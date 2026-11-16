# 测试文件说明

这个目录包含了两个手工测试脚本（需要真实的 Aria2 / Quark 环境），用于测试项目的各个 API 组件。

自动化测试（无需外部服务）在 `tests/pytest/` 目录下：`python -m pytest`。

> **不参与 pytest 收集**：这里的入口函数叫 `check_aria2_api()` / `check_quark_api()`（不是 `test_*`），
> 且 `pytest.ini` 的 `testpaths` 只指向 `tests/pytest`。因此 `python -m pytest` 不会跑到这两个脚本——
> 它们会读真实 `conf/` 配置、真连 Aria2 / 夸克账号，且异常被吞掉后仍算“通过”，当测试用会给出假绿灯。
> 只在本机手工执行：`python tests/munual/test_aria2.py`。

## 测试文件列表

### 1. test_aria2.py
测试 Aria2 下载服务API功能。

**测试内容：**
- 获取当前正在执行的任务
- 获取等待中的任务
- 获取已完成的任务
- 获取暂停/错误/已移除的任务

**使用方法：**
```bash
python tests/munual/test_aria2.py
```

**配置要求：**
- 需要运行中的 Aria2 服务
- 在代码中配置正确的 `aria2_host` 和 `aria2_secret`

### 2. test_quark.py
测试 Quark 网盘API功能。

**测试内容：**
- 获取网盘可用空间
- 列出网盘中的所有文件
- 统计文件类型和大小
- 计算总容量和使用情况

**使用方法：**
```bash
python tests/munual/test_quark.py
```

**配置要求：**
- 需要有效的 Quark 网盘 cookie
- 在代码中配置正确的 `cookie` 等参数

## 注意事项

1. **配置参数**：每个测试文件中的配置参数都是示例值，需要根据实际情况修改。

2. **依赖服务**：运行测试前确保相关服务（Aria2、Quark）正在运行。

3. **权限要求**：Quark API 需要有效的登录凭据。

4. **网络连接**：确保网络连接正常，能够访问相关服务。

## 输出示例

每个测试文件都会输出详细的测试结果，包括：
- 任务状态统计
- 文件列表
- 容量使用情况
- 错误信息（如果有）

## 故障排除

如果测试失败，请检查：
1. 服务是否正在运行
2. 配置参数是否正确
3. 网络连接是否正常
4. 认证信息是否有效
