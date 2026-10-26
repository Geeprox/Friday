# 导入自己的米家房型

Friday 没有内置家庭房型。`house-model.js` 的 defaultRooms 为空；房间边界、孔洞、
设备身份与位置来自本地导入。渲染样式是项目代码，真实几何不是示例源码。

从米家取得自己的楼层数据后先预览：

```sh
python3 scripts/embodied/import_home_layout.py --floormap-json /path/to/your-floor.json \
  --output data/embodied/private/home-layout.json --preview
```

BakeData 输入也受支持：

```sh
python3 scripts/embodied/import_home_layout.py --baked-json /path/to/floor_data.json \
  --texture /path/to/texture.bmp --room-names /path/to/room-names.json \
  --output data/embodied/private/home-layout.json --preview
```

核对预览后去掉 `--preview`，并用 `--expected-revision` 指定预览的当前版本；
`--help` 查看设备位置变更的独立确认参数。
Android 获取需要 ADB 与本人 USB 授权，只读取所选米家 BakeData 楼层文件；不保证
每个米家版本/手机系统都开放该目录，不自动绕过系统权限。

归一化保持源数据的 XZ 相对坐标、凹多边形与孔洞，不创造米制尺寸、设备位置或
指南针朝向。缺失设备位置保留未放置。重复导入按稳定 ID 保留已经核对的位置，
修改使用 revision 检查，避免覆盖较新的导入。所有原始导出、图片与生成 JSON
放在忽略的 data/，不要作为 bug 附件提交；报告问题时制作一个新的合成房型。
