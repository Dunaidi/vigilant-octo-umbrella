# 作业帮「比大小」口算 PK 自动化（雷电模拟器）

基于 ADB + UI dump 的 Windows 脚本，在雷电模拟器中自动完成作业帮口算竞技场「比大小」对局。开局前预读整局题目并预建输入通道，开局后按判定结果批量划线提交；支持连续多局、空格暂停。额外处理常见中断：人机验证页的加减法填写、口算段位升级弹窗的「确定」。日志默认写入 `zyb_logs/`，可用 `--check` 只做设备与界面自检。

面向本地模拟器自动化学习与实验；使用前请自行确认是否符合作业帮用户协议与当地法规，风险自负。

## 环境

- Windows
- [雷电模拟器](https://www.ldmnq.com/)（默认路径含 `adb.exe` / `ldconsole.exe`）
- Python 3.10+（直接跑源码时）；或使用自行打包的 exe

## 成品下载

可直接从 [Releases](https://github.com/Dunaidi/vigilant-octo-umbrella/releases) 下载打包好的 `zyb_bidaxiao_pk.exe`（Windows，需本机已安装并可用雷电模拟器 / ADB）。

## 快速开始

```bash
# 源码
python zyb_bidaxiao_pk.py --check
python zyb_bidaxiao_pk.py
python zyb_bidaxiao_pk.py --rounds 0

# 或运行 Release 中的 exe
zyb_bidaxiao_pk.exe
```

空格暂停，Ctrl+C 停止。

## 常用参数

| 参数 | 说明 |
|------|------|
| `--check` | 只检查 ADB / 设备 / 界面 |
| `--rounds N` | 局数；`0` 为持续运行 |
| `--strategy batch\|checked` | 整局批量提交 / 逐题确认 |
| `--answer-gap` | 批量题间间隔（秒），漏题时可加大 |
| `--serial` | 多设备时指定 ADB 序列号 |
| `--log-dir` | 日志目录，默认 `./zyb_logs` |

## 贡献者

- [Dunaidi](https://github.com/Dunaidi)
- [Codex](https://openai.com/codex)（协助开发）

## License

MIT
