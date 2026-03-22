import requests
import qrcode
import time
import json
import sys
import os
import re
from datetime import datetime

def log(msg):
    """带时间戳的全局日志输出"""
    print(f"[{datetime.now().strftime('%m-%d %H:%M:%S')}] {msg}")

# 雨课堂基础配置 - 伪装为微信UA(好像没什么必要，但还是加上了)
# 请在此修改你的核心学校版雨课堂域名
BASE_URL = "https://changjiang.yuketang.cn"  
WECHAT_UA = "Mozilla/5.0 (Linux; Android 10; SM-G975F) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/81.0.4044.138 Mobile Safari/537.36 MMWEBID/1602 MicroMessenger/8.0.30(0x28001E55) Process/tools NetType/WIFI Language/zh_CN ABI/arm64"

HEADERS = {
    "User-Agent": WECHAT_UA,
    "xtbz": "ykt",
    "Referer": f"{BASE_URL}/v2/web/index",
    "Content-Type": "application/json"
}

class YuketangHelper:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update(HEADERS)
        self.sessionid = None
        self.csrftoken = None
        self.classroom_id = None

    def save_session(self):
        cookies = self.session.cookies.get_dict()
        session_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "yuketang_session.json")
        with open(session_file, "w") as f:
            json.dump(cookies, f)
        log(f"[*] 登录凭证已保存至 {session_file}！")

    def load_session(self):
        session_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "yuketang_session.json")
        try:
            with open(session_file, "r") as f:
                cookies = json.load(f)
                self.session.cookies.update(cookies)
                self.sessionid = cookies.get('sessionid')
                if 'csrftoken' in cookies:
                    self.csrftoken = cookies['csrftoken']
            
            if self.sessionid:
                log(f"[*] 读取到本地凭证 ({session_file})，正在向云端校验存活状态...")
                url = f"{BASE_URL}/api/v3/classroom/on-lesson-upcoming-exam"
                self.session.headers.update({"X-CSRFToken": self.csrftoken})
                resp = self.session.get(url, timeout=10)
                data = resp.json()
                
                # 校验：返回正规接口结构且属于登录状态
                if isinstance(data, dict) and (data.get("code") == 0 or data.get("success")):
                    log("[+] 凭证效验通过，成功恢复免扫码状态！")
                    return True
                else:
                    log("[-] 本地凭证在服务器端已失效或被顶号，需要重新扫码登录。")
                    self.sessionid = None
                    return False
            return False
        except FileNotFoundError:
            # 静默处理首次运行
            return False
        except Exception as e:
            log(f"[-] 读取或校验凭证时发生致命错误: {e}")
            return False

    def _get_wechat_uuid(self, login_url):
        """核心解析: 从微信授权页面提取原生 UUID"""
        try:
            target_url = login_url + "&login_type=jssdk&self_redirect=true"
            resp = requests.get(target_url, headers={"User-Agent": WECHAT_UA})
            match = re.search(r'src="/connect/qrcode/([^"]+)"', resp.text)
            if match:
                return match.group(1)
            else:
                return None
        except Exception as e:
            log(f"[!] 提取微信 UUID 出错: {e}")
            return None

    def get_login_qrcode(self):
        """步骤 1: 向雨课堂申请扫码登录参数并显示二维码"""
        log("[*] 正在向雨课堂申请登录授权参数...")
        url = f"{BASE_URL}/api/v3/user/login/wechat-auth-param"
        try:
            resp = self.session.post(url, json={})
            self.csrftoken = resp.cookies.get('csrftoken')
            
            data = resp.json()
            if data['code'] != 0:
                log(f"[!] 获取授权参数失败: {data['msg']}")
                return None, None
            
            auth_data = data['data']
            app_id = auth_data['appId']
            state = auth_data['state']
            redirect_uri = auth_data['redirectUri']
            
            login_url = f"https://open.weixin.qq.com/connect/qrconnect?appid={app_id}&redirect_uri={redirect_uri}&response_type=code&scope=snsapi_login&state={state}"
            
            log("[*] 正在解析原生微信登录凭证...")
            uuid = self._get_wechat_uuid(login_url)
            
            if not uuid:
                log("[!] 无法获取 UUID，尝试使用降级方案显示二维码。")
                final_qr_content = login_url + "#wechat_redirect"
                return state, None
            
            # 使用原生的确认登录协议渲染二维码
            final_qr_content = f"https://open.weixin.qq.com/connect/confirm?uuid={uuid}"
            print("\n" + "="*50)
            log("[*] 请使用微信扫描下方二维码进行登录确认：")
            qr = qrcode.QRCode()
            qr.add_data(final_qr_content)
            qr.make(fit=True)
            qr.print_ascii(invert=True)
            print("="*50 + "\n")
            
            return state, uuid
        except Exception as e:
            log(f"[!] 获取二维码失败 (错误: {e})")
            return None, None

    def wait_for_login_and_callback(self, state, uuid):
        """核心重构: 轮询微信官方接口获取 code, 并访问雨课堂回调接口完成登录"""
        if not uuid:
            log("[!] 缺少 UUID，无法在后台追踪扫码状态。")
            return False
            
        # 1. 轮询微信接口获取授权 code
        log("[*] 正在轮询微信状态，等待手机确认...")
        poll_url = f"https://lp.open.weixin.qq.com/connect/l/qrconnect?uuid={uuid}&_={int(time.time()*1000)}"
        
        while True:
            try:
                # 微信轮询不需要复杂的 Header
                resp = requests.get(poll_url, timeout=30)
                content = resp.text
                
                # 微信返回的是 JS 代码，格式如: window.wx_errcode=XXX;window.wx_code='...';
                errcode_match = re.search(r'window.wx_errcode=(\d+)', content)
                if not errcode_match:
                    continue
                
                errcode = int(errcode_match.group(1))
                if errcode == 405: # 用户已确认登录
                    code_match = re.search(r"window.wx_code='([^']+)'", content)
                    if code_match:
                        auth_code = code_match.group(1)
                        log("[+] 微信授权成功，获得临时票据。")
                        return self._finalize_login(auth_code, state)
                elif errcode == 408: # 超时继续轮询
                    pass
                elif errcode == 404: # 已扫码待确认
                    log("[*] 扫码成功，请在手机上点击确认...")
                elif errcode == 403: # 二维码真正的失效状态码通常是 403 或其他
                    log("[!] 二维码可能已失效，请重新运行脚本。")
                    return False
                
                # 更新时间戳以防缓存
                poll_url = f"https://lp.open.weixin.qq.com/connect/l/qrconnect?uuid={uuid}&_={int(time.time()*1000)}"
                time.sleep(2)
            except KeyboardInterrupt:
                return False
            except Exception as e:
                log(f"[!] 轮询微信接口异常: {e}")
                time.sleep(5)

    def _finalize_login(self, code, state):
        """步骤 3: 访问雨课堂回调接口，将微信凭证兑换为 sessionid"""
        log("[*] 正在进行雨课堂最终握手登录...")
        callback_url = f"{BASE_URL}/api/v3/user/login/wechat-web-callback"
        params = {"code": code, "state": state}
        
        try:
            # 访问回调接口（禁止自动跳转，拦截真实的 302 回调指令防 Cookie 蒸发）
            resp = self.session.get(callback_url, params=params, allow_redirects=False)
            
            # 使用全局 Session 获取最新 Cookie
            cookies_dict = self.session.cookies.get_dict()
            self.sessionid = cookies_dict.get('sessionid')
            self.csrftoken = cookies_dict.get('csrftoken') or self.csrftoken
            
            if self.sessionid:
                log("[+] 登录全流程完成！已成功捕获凭据。")
                self.save_session()
                return True
            else:
                log(f"[!] 登录失败，服务器未返回 sessionid。")
                log(f"|--- HTTP 状态码: {resp.status_code}")
                log(f"|--- 截获的 Cookie: {cookies_dict}")
                log(f"|--- Response Headers: {resp.headers}")
                return False
        except Exception as e:
            log(f"[!] 最终登录环节出错: {e}")
            return False

    def get_active_lesson_data(self):
        """步骤 4: 自动发现活跃课堂"""
        log("[*] 正在自动检索当前正在进行的课堂...")
        url = f"{BASE_URL}/api/v3/classroom/on-lesson-upcoming-exam"
        try:
            self.session.headers.update({"X-CSRFToken": self.csrftoken})
            resp = self.session.get(url)
            data = resp.json()
            active_list = data.get('data', {}).get('onLessonClassrooms', [])
            if not active_list:
                log("[-] 目前没有检测到正在进行的课堂。")
                return None, None
            found = active_list[0]
            return found.get('lessonId'), found.get('classroomId')
        except Exception as e:
            log(f"[!] 获取课堂列表失败: {e}")
            return None, None

    def sign_in(self, lesson_id, classroom_id=None, source=1):
        """步骤 5: 执行伪装签到"""
        url = f"{BASE_URL}/api/v3/lesson/checkin"
        
        # 极度危险的坑：如果 lesson_id 是 1646245271787354752 这种19位超大 Snowflake ID，
        # 在 JSON 中裸传数字会被 JS 后端解析成 1646245271787354800 导致精度丢失和找不到课！
        # 这里自动将其强转为字符串以防范后端溢出。
        safe_lesson_id = str(lesson_id)
        
        # 极度隐蔽的坑2：V3接口要求驼峰命名法 'lessonId'，如果你传 'lesson_id'，后端收到的就是 null！
        payload = {"lessonId": safe_lesson_id, "source": source}
        
        headers = {
            "X-CSRFToken": self.csrftoken, 
            "xtbz": "ykt", 
            "User-Agent": WECHAT_UA,
            "Referer": f"{BASE_URL}/v2/web/index"
        }
        if classroom_id:
            headers["Referer"] = f"{BASE_URL}/v2/web/studentLog/{classroom_id}"
        
        log(f"[*] 正在伪装微信提交签到 (Lesson: {lesson_id})...")
        try:
            resp = self.session.post(url, headers=headers, json=payload)
            result = resp.json()
            if result.get('code') == 0:
                log(f"[SUCCESS] 签到完成: {result.get('msg')}")
            else:
                log(f"[FAILED] 失败: {result.get('msg')}")
        except Exception as e:
            log(f"[ERROR] 异常: {e}")

    def keep_alive(self):
        """定期访问核心接口以重置 Session 的过期时间"""
        # 改用我们确信结构稳定的课堂巡检接口作为底层心跳包
        url = f"{BASE_URL}/api/v3/classroom/on-lesson-upcoming-exam"
        try:
            self.session.headers.update({"X-CSRFToken": self.csrftoken})
            resp = self.session.get(url)
            data = resp.json()
            
            # 如果服务端正常返回 JSON 格式且状态码验证身份为有效（code=0 或 success状态）
            if isinstance(data, dict) and (data.get("code") == 0 or data.get("success") == True):
                log("[+] 账户保活成功：当前状态已向雨课堂云端重置 (Keep-Alive)。")
                self.save_session()
                return True
            else:
                log("[-] 保活状态效验异常，Session 可能已过期。")
                return False
        except Exception as e:
            log(f"[!] 保活心跳包请求异常: {e}")
            return False

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="雨课堂(Yuketang) 一键自动签到脚本")
    parser.add_argument("-a", "--auto", action="store_true", help="一键全自动：搜索活跃课程并签到")
    parser.add_argument("-l", "--lesson", type=str, help="指定目标 Lesson ID 进行签到")
    parser.add_argument("-s", "--source", type=int, default=1, help="指定模拟签到的 Source 参数 (默认: 1)")
    parser.add_argument("-k", "--keepalive", action="store_true", help="单次保活模式：刷新凭证有效期后退出")
    args = parser.parse_args()

    helper = YuketangHelper()
    
    # 首先尝试热加载本地凭证
    logged_in = helper.load_session()
    
    # 如果没加载到，或者失效，则进入扫码流程
    if not logged_in:
        state, uuid = helper.get_login_qrcode()
        if state and helper.wait_for_login_and_callback(state, uuid):
            logged_in = True
            
    if logged_in:
        # 如果带有命令行一键打卡参数，则执行后直接退出，作为定时任务的无头模式
        if args.keepalive:
            log("[*] CLI模式: 正在执行保活...")
            helper.keep_alive()
            sys.exit(0)

        if args.auto:
            l_id, c_id = helper.get_active_lesson_data()
            if l_id:
                log(f"[*] CLI模式: 发现活跃课程 {l_id}，正在以 source={args.source} 执行签到...")
                helper.sign_in(l_id, classroom_id=c_id, source=args.source)
            else:
                log("[-] 自动巡检未发现正在进行的课堂。")
            sys.exit(0)
            
        if args.lesson:
            log(f"[*] CLI模式: 正在强制进入指定课程 {args.lesson}，source={args.source}...")
            helper.sign_in(args.lesson, classroom_id=None, source=args.source)
            sys.exit(0)

        # 没有传递参数时，退回原来的交互菜单
        log("\n[V] 登录会话已就绪！您现在可以反复尝试签到功能，不必重新扫码。")
        while True:
            print("\n" + "-"*50)
            print("【雨课堂辅助菜单】")
            print("1. 自动扫描课程并用 source=1 签到")
            print("2. 自动扫描课程，手动输入 source")
            print("3. 手动输入 lesson_id 和 source")
            print("4. 退出脚本")
            
            try:
                choice = input("请输入操作对应的数字 (1/2/3/4): ").strip()
                if choice == "1":
                    l_id, c_id = helper.get_active_lesson_data()
                    if l_id:
                        log(f"[*] 发现课程 {l_id}，正在以微信服务号形式 (source=1) 尝试签到...")
                        helper.sign_in(l_id, classroom_id=c_id, source=1)
                    else:
                        log("[-] 没有发现活跃课堂，请检查当前是否有课。")
                elif choice == "2":
                    l_id, c_id = helper.get_active_lesson_data()
                    if l_id:
                        print("\n[Source 参数指引] \n 1 扫二维码 (默认)\n 6 课堂暗号\n 9 小程序分享\n 2-5/其他 “正在上课”提示")
                        req_source = input("\n请输入要使用的 source 值 [默认 1]: ").strip()
                        req_source = int(req_source) if req_source.isdigit() else 1
                        log(f"[*] 发现课程 {l_id}，正在尝试使用 source={req_source} 签到...")
                        helper.sign_in(l_id, classroom_id=c_id, source=req_source)
                    else:
                        log("[-] 没有发现活跃课堂，请检查当前是否有课。")
                elif choice == "3":
                    target_id = input("请输入目标 Lesson ID: ").strip()
                    if target_id:
                        print("\n[Source 参数指引] \n 1 扫二维码 (默认)\n 6 课堂暗号\n 9 小程序分享\n 2-5/其他 “正在上课”提示")
                        req_source = input("\n请输入要使用的 source 值 [默认 1]: ").strip()
                        req_source = int(req_source) if req_source.isdigit() else 1
                        helper.sign_in(target_id, classroom_id=None, source=req_source)
                elif choice == "4":
                    log("[*] 结束退出。您的本地登录凭证仍在有效期内！下次启动可免扫码。")
                    sys.exit(0)
                else:
                    print("[!] 无效的选择，请重新输入。")
            except KeyboardInterrupt:
                log("监测到终端退出指令")
                sys.exit(0)
            except Exception as e:
                # 捕获未知异常，防止死循环崩盘
                log(f"[!] 发生意外错误，但已阻止强制退出: {e}")
