# 快速启动

一个 Windows 11 常驻系统托盘的快捷启动器。

按 `Ctrl + Shift + Alt + K` 打开界面，读取用户开始菜单第一层的快捷方式，显示图标，
支持键盘选择和搜索。使用过的项目会自动排到最顶部。

## 功能

- 全局快捷键：默认为 `Ctrl + Shift + Alt + K`，可在托盘菜单中自定义，保存后立即生效
- 只扫描 `%APPDATA%\Microsoft\Windows\Start Menu` **目录本身**，不进入任何子文件夹
  （因此不会读取 `Programs`、`Startup` 等子目录）
- 支持 `.lnk`、`.url`、`.appref-ms` 三种快捷方式
- 打开时窗口铺满当前屏幕（保留任务栏），列表显示 96 px 大图标
- 图标直接取自系统高清图像列表（最高 256 px），放大后依然清晰
- 自动去掉文件名末尾的 “- 快捷方式 / - Shortcut”
- `↑` / `↓` 选择（列表随窗口高度滚动），`Enter` 或鼠标单击打开，`Esc` 或点击窗口外关闭
- 输入文字可即时过滤（支持空格分隔的多个关键字）
- 打开过的快捷方式自动移到顶部，并保存到 `%APPDATA%\StartMenuLauncher\settings.json`
- 系统托盘常驻：左键打开；右键菜单可打开 / 刷新应用列表 / 设置全局快捷键 / 重启 / 退出
  （菜单打开时按 `R` 重启、按 `X` 退出）
- 单实例运行：重复启动只会唤起已经运行的窗口

## 安装

需要 Windows 11、Python 3.10 或更高版本。

```powershell
cd D:\files\projects\tmp1
python -m pip install -r requirements.txt
```

## 启动

静默启动（推荐，不显示控制台窗口）：

```powershell
pythonw .\start_menu_launcher.py
```

调试时用 `python .\start_menu_launcher.py`，可以在控制台看到报错信息。

启动后不会立刻弹出窗口，这是正常的；程序会待在系统托盘里。按快捷键即可打开。
如果快捷键被其他程序占用，托盘会弹出提示，退出冲突程序后重启本程序即可。

## 开机自动启动（可选）

1. 按 `Win + R`，输入 `shell:startup` 并回车。
2. 在打开的启动文件夹里新建一个快捷方式，目标填写：

   ```text
   "C:\Users\92892\AppData\Local\Programs\Python\Python313\pythonw.exe" "D:\files\projects\tmp1\start_menu_launcher.py"
   ```

   起始位置填写 `D:\files\projects\tmp1`。

## 文件说明

- `start_menu_launcher.py`：主程序
- `requirements.txt`：Python 依赖（PyQt6）
- 配置：`%APPDATA%\StartMenuLauncher\settings.json`

## 修改快捷键

推荐在系统托盘的右键菜单中选择「设置全局快捷键…」，点击输入框后直接按下新的组合键，
再点「保存」即可，无需重启程序。快捷键必须包含 `Ctrl`、`Alt` 或 `Win` 键；
如果组合键已被其他程序占用，界面会给出提示并保留原来的快捷键。

也可以直接编辑 `%APPDATA%\StartMenuLauncher\settings.json` 中的 `hotkey` 字段
（例如 `"hotkey": "Ctrl+Shift+Space"`），然后从托盘菜单重启程序。
