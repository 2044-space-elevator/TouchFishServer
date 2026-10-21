"""
没错，只是为了放验证码和邮箱验证的程序
"""

import os
from concurrent.futures import ThreadPoolExecutor
from json_store import read_json, update_json, write_json
from random import choices, randint
import smtplib
from string import ascii_letters, digits
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.header import Header
from threading import BoundedSemaphore
from time import time

MAX_DELAY = 300

def login_email(sender_email : str, password : str, smtp_host=None, smtp_port=None, smtp_use_ssl=True):
    """
    password 是登录密码或者授权码，视邮件提供商要求
    smtp_host/smtp_port/smtp_use_ssl 可手动指定 SMTP 参数；
    未指定 smtp_host 时按邮箱域名自动猜测 smtp.<domain>
    smtp_use_ssl: True = SMTP_SSL 直连（默认 465），False = SMTP + STARTTLS（默认 587）
    """
    if smtp_host:
        smtp_server = smtp_host
    else:
        smtp_server = 'smtp.' + sender_email.split('@')[1]
    if smtp_port is None:
        smtp_port = 465 if smtp_use_ssl else 587
    if smtp_use_ssl:
        server = smtplib.SMTP_SSL(smtp_server, smtp_port, timeout=10)
    else:
        server = smtplib.SMTP(smtp_server, smtp_port, timeout=10)
        server.starttls()
    server.login(sender_email, password)
    return server

def send_email(server, sender_email : str, receiver_email : str, subject : str, content : str):
    """
    server 是 smtplib.SMTP_SSL 对象
    """
    msg = MIMEMultipart()
    msg["From"] = sender_email
    msg["To"] = receiver_email
    msg["Subject"] = Header(subject, 'utf-8')
    msg.attach(MIMEText(content, "plain", "utf-8"))
    try:
        server.sendmail(sender_email, receiver_email, msg.as_string())
    except:
        return False
    return True

def delete_old_captcha(port_api : int):
    folder_path = "res/{}/captcha".format(port_api)
    current_time = time()
    for filename in os.listdir(folder_path):
        file_path = os.path.join(folder_path, filename)
        if "captcha.json" in file_path:
            continue
        if os.path.isfile(file_path):
            try:
                create_time = int(filename.split('.')[0])
                if current_time - create_time > MAX_DELAY:
                    os.remove(file_path)
            except:
                continue

def generate_captcha(port_api : int, Imgcaptcha, lock):
    """
    生成验证码
    ImgCaptcha 是 captcha.ImageCaptcha 对象
    返回的是验证码时间戳，可以当做是 token
    """
    delete_old_captcha(port_api)
    chars = ascii_letters + digits
    answer =  choices(chars, k=6)
    captcha_text = ''.join(answer)
    time_now = int(time())
    Imgcaptcha.write(captcha_text, 'res/{}/captcha/{}.png'.format(port_api, time_now))
    with lock:
        update_json(
            "res/{}/captcha/captcha.json".format(port_api),
            lambda cap: cap.__setitem__(str(time_now), captcha_text),
        )
    return time_now

def verify_captcha(port_api : int, time_stamp : int, verify_text : str, lock):
    """
    校验验证码（校验一次之后就报废啦）
    """
    try:
        stamp = int(time_stamp)
    except (TypeError, ValueError):
        return False, False
    if time() - stamp > MAX_DELAY:
        return False, False
    folder_path = "res/{}/captcha".format(port_api)
    png_name = "{}.png".format(stamp)
    if png_name not in os.listdir(folder_path):
        return False, False
    with lock:
        lst = read_json(folder_path + '/captcha.json')
        answer = lst.pop(str(stamp), None)
        if answer is None:
            return False, False
        write_json("res/{}/captcha/captcha.json".format(port_api), lst)
        try:
            os.remove(os.path.join(folder_path, png_name))
        except OSError:
            pass
        return answer.lower() == str(verify_text).lower(), True

def issue_email_code(port_api : int, email : str, activate_lock):
    """生成验证码并同步写入 activate.json，返回验证码 必须在请求返回前完成
    """
    verify_code = randint(100000, 999999)
    with activate_lock:
        update_json(
            "res/{}/activate.json".format(port_api),
            lambda activate_lst: activate_lst.__setitem__(email, verify_code),
        )
    return verify_code


def send_email_code(sender_email : str, port_api : int, email : str, password : str, config_lock, verify_code : int):
    """发送已生成的验证码"""
    folder_path = "res/{}".format(port_api)
    with config_lock:
        cfg = read_json(folder_path + '/config.json')
        server_name = cfg["server_name"]
        smtp_host = cfg.get("smtp_host", "") or None
        smtp_port = cfg.get("smtp_port")
        smtp_use_ssl = cfg.get("smtp_use_ssl", True)
    session = login_email(sender_email, password, smtp_host, smtp_port, smtp_use_ssl)
    return send_email(session, sender_email, email, "{} 验证码".format(server_name), "欢迎使用 {}，您的验证码是 {}。".format(server_name, verify_code))


def email_code(sender_email : str, port_api : int, email : str, password : str, config_lock, activate_lock):
    verify_code = issue_email_code(port_api, email, activate_lock)
    return send_email_code(sender_email, port_api, email, password, config_lock, verify_code)


# 避免被刷爆 IO
_EMAIL_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="tfv5-smtp")
_EMAIL_PENDING = BoundedSemaphore(32)


def _send_email_code_logged(sender_email, port_api, email, password, config_lock, verify_code):
    try:
        ok = send_email_code(sender_email, port_api, email, password, config_lock, verify_code)
        if not ok:
            print("[WARN] 验证码邮件发送失败: {}".format(email))
    except Exception as e:
        print("[WARN] 验证码邮件发送异常({}): {}".format(email, e))
    finally:
        _EMAIL_PENDING.release()


def email_code_async(sender_email, port_api, email, password, config_lock, activate_lock):
    """
    写入验证码并后台发信
    False 表示排队已满或验证码写入失败
    """
    if not _EMAIL_PENDING.acquire(blocking=False):
        print("[WARN] 验证码邮件队列已满，拒绝本次请求: {}".format(email))
        return False
    try:
        verify_code = issue_email_code(port_api, email, activate_lock)
    except Exception as e:
        _EMAIL_PENDING.release()
        print("[WARN] 验证码写入失败({}): {}".format(email, e))
        return False
    try:
        _EMAIL_EXECUTOR.submit(
            _send_email_code_logged,
            sender_email, port_api, email, password, config_lock, verify_code,
        )
    except Exception as e:
        _EMAIL_PENDING.release()
        print("[WARN] 验证码邮件入队失败: {}".format(e))
        return False
    return True

def verify_email(port_api : int, email : str, code : int, lock):
    if not isinstance(code, int) or code < 0:
        return False
    folder_path = "res/{}/".format(port_api)
    with lock:
        def verify(code_lst):
            if code_lst.get(email) != code:
                return False
            del code_lst[email]
            return True
        return update_json(folder_path + '/activate.json', verify)
