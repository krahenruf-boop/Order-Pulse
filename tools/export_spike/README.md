# 人工导出核验工具（不是应用Connector）

仅用于已授权账号、已核验模板和有界付款范围。不要直接当后台任务使用，不会自动停旧系统，也不安装浏览器权限。

- `observe.js`：被动观察正常UI的XHR，仅保存白名单请求字段和返回结构；没有请求头/买家行数据。调用返回的restore解除，另有15分钟自动恢复。
- `download_probe.mjs`：在人工已准备好的导出对话框点击“导出”，在同一浏览器操作里临时捕获XLS弹窗，避免额外Downloads副本；从本次响应真实地址下载到唯一临时目录，执行解析后finally清理并恢复钩子。没有私有API重放。
- `inspect_xls.py`：真实XLS检查，只输出schema、计数、聚合、类型及哈希；不是完整产品统计实现。
- `test_inspect_xls.py`：合成单元格测试，不调用网站、不生成演示XLS。

## 隔离依赖及测试

xlrd2.0.2安装到`/tmp/order-pulse-spike-deps`，不修改应用requirements。使用有pip的独立Python解释器：

```sh
python3 -m pip install --target /tmp/order-pulse-spike-deps xlrd==2.0.2
python3 tools/export_spike/test_inspect_xls.py
```

## 浏览器探针前提

先按ego-browser文档核对空间ownership、账号、TMS页面、旧任务空闲窗口。明确选择模板1并检查允许列，禁止选择买家字段、修改保存模板或盲点其他批量操作。

探针由人工核验在页面建立临时`__opSpikeVue`（当前已显示订单组件）、`__opSpikeAllFiltered`（仅用于已核验完成条件导出），然后执行`observe.js`。这是依赖现场页面的测试辅助，页面结构改变时停止，不猜选择器。

`/tmp/order-pulse-spike-options.json`由测试者在本机创建，内容包括：

```json
{
  "project_root": "/absolute/path/to/Order-Pulse",
  "python_path": "/absolute/path/to/python3",
  "space_id": 1,
  "page_label": "p1",
  "test_name": "bounded_sample",
  "existing_export": false
}
```

`test_name`仅由测试者填写安全文件名。`existing_export=true`仅用于同轮已实际返回URL的文件重读核验，不能作为重试失败导出的方式。完成后删除临时options文件、解除页面观察器。不要把完整URL或真实XLS放进Git。

本轮首次浏览器download事件等待失败，初始URL下载还产生了额外浏览器副本，后经哈希核对删除。最终同轮弹窗捕获+响应URL下载已实测1单通过；大批量及完成来源受控下载尚未补测。脚本不是“原生download事件成功”的证据。正式租约/重启清理/来源fallback仍未开发。
