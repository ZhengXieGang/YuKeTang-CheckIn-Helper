这是一个专为长江雨课堂打造的签到脚本，别的雨课堂简单改改就能用。
可实现自动签到以及伪装签到方式。
登入过一次后，自动生成json文件储存SessionId，下次运行免扫码，只要SessionId没过期就能长期用。

## 启动指引

### 1. 准备环境
请确保安装好必要的依赖包：
```bash
pip install requests qrcode
```

### 2. 运行脚本
```bash
python yuketang_helper.py
```
> 首次运行请用微信扫描二维码登录


### 3. 自动化命令行模式 (CLI)
脚本全面支持命令行的无头（Headless）带参数启动，非常适合配合软路由或服务器做定时挂机：
    `python yuketang_helper.py -a`
    *(自动发现正在讲授的课堂，以极安全身份秒速签到后退出进程)*
    `python yuketang_helper.py -a -s [source]`
    *(强制用课堂暗号的 source=[source] 环境去扫描打卡)*
    `python yuketang_helper.py -l [lesson_id] -s [source]`
    *(无论是否上课，直接对指定的课程 ID 用网页端权限进行强制签到记录)*
    `python yuketang_helper.py -k`
    *(sessionid保活：刷新凭证有效期后退出)*

---
*声明：本脚本所有逆向经验仅供自动化协议学习研究，请遵守各高校考勤管理相关要求。*
