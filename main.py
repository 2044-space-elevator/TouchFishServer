import argparse
import json
import asyncio
from argon2 import PasswordHasher
from channel import InstantConnect
from captcha.image import ImageCaptcha
import threading
import web
from json_store import update_json, write_json
import db
from db.dialect import SQLiteDialect, MySQLDialect, PostgreSQLDialect
import avatar
from file import init, collect_expired
import logging
from crypto import generate_rsa_keys, load_pub, load_pri
import time
import os
import sys

ASCII_LOGO = """
####### #######    #     # #######   ##  #####                                     ##   
   #    #          #     # #        #   #     # ###### #####  #    # ###### #####    #  
   #    #          #     # #       #    #       #      #    # #    # #      #    #    # 
   #    #####      #     # ######  #     #####  #####  #    # #    # #####  #    #    # 
   #    #           #   #        # #          # #      #####  #    # #      #####     # 
   #    #            # #   #     #  #   #     # #      #   #   #  #  #      #   #    #  
   #    #             #     #####    ##  #####  ###### #    #   ##   ###### #    # ##   
"""

COLORS = \
{
	"black": 0,
	"red": 1,
	"green": 2,
	"yellow": 3,
	"blue": 4,
	"magenta": 5,
	"cyan": 6,
	"white": 7
}

# 染色函数

def dye(text, color_code):
	if color_code:
		return "\033[0m\033[1;3{}m{}\033[8;30m\033[0m".format(COLORS[color_code], text)
	return text

def prt(plain, color=None):
	print(dye(plain, color))

PORT_API = None
PORT_TCP = None
PUB_KEY = None
PRI_KEY = None
FLASK_APP = None
FLASK_THREAD = None
TCP_THREAD = None
INSTANT_CONTACT = None
IMGCAPTCHA = None
HASHER = None
ENABLE_DEBUG = False
ENABLE_WERKZEUG_LOG = False
USE_DEV_SERVER = False
WAITRESS_THREADS = 16
WAITRESS_CONNECTION_LIMIT = 1000

# Sql 游标
# 节省资源，一个游标用到天荒地老

FORUM_CURSOR = None
USER_CURSOR = None


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="TouchFish V5 server")
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument("--create-new-config", action="store_true", help="创建新的服务器配置")
    mode_group.add_argument("--use-config", help="使用指定编号的服务器配置")
    parser.add_argument("--start-api", action="store_true", help="直接启动内置 API 服务器")
    parser.add_argument("--dev-server", action="store_true", help="使用 Flask 开发服务器而非 waitress（仅调试用）")
    parser.add_argument("--debug", action="store_true", help="启动内置 API 服务器时启用 Flask debug（仅 --dev-server 有效）")
    parser.add_argument("--log", action="store_true", help="启动内置 API 服务器时输出 Werkzeug 日志（仅 --dev-server 有效）")
    parser.add_argument("--waitress-threads", type=int, default=16, help="waitress 工作线程数（默认 16，仅 waitress 有效）")
    parser.add_argument("--waitress-connection-limit", type=int, default=1000, help="waitress 最大并发连接数（默认 1000，仅 waitress 有效）")
    args = parser.parse_args(argv)
    args.cli_mode = len(sys.argv) > 1 if argv is None else len(argv) > 0
    return args


def normalize_working_directory():
    os.chdir(os.path.dirname(os.path.abspath(__file__)))


def create_new_server():
    global HASHER
    global PUB_KEY, PRI_KEY
    global PORT_API
    global PORT_TCP
    global USER_CURSOR

    HASHER = PasswordHasher(
        time_cost=2,
        memory_cost=65536,
        parallelism=2,
        hash_len=24,
        salt_len=16
    )
    PORT_API = input("请输入新服务器的 API 端口号（0~65535且不受防火墙阻挡）：")
    PORT_TCP = input("请输入新服务器的 TCP 端口号（0~65535且不受防火墙阻挡且不与上者重复）：")
    pri, pub, pri_pem, pub_pem, has = generate_rsa_keys()
    prt("你的 RSA 公钥 SHA256 哈希值：{}，为防止中间人攻击导致的危险，请务必将其公布于受信任处以让使用者校对！".format(has), "yellow")
    if not os.path.exists("res/{}/secret".format(PORT_API)):
        os.makedirs("res/{}/secret".format(PORT_API))

    if not os.path.exists("res/{}/captcha".format(PORT_API)):
        os.makedirs("res/{}/captcha".format(PORT_API))
    
    with open("res/{}/secret/pub.pem".format(PORT_API), "wb") as file:
        file.write(pub_pem)
    with open("res/{}/secret/pri.pem".format(PORT_API), "wb") as file:
        file.write(pri_pem)
    
    def add_server(config):
        next_id = max((int(key) for key in config if str(key).isdigit()), default=-1) + 1
        config[str(next_id)] = [PORT_API, PORT_TCP]
    update_json("server_config.json", add_server, default=dict)

    print("创建数据库与配置文件……")
    if not os.path.exists("res/{}/db".format(PORT_API)):
        os.makedirs("res/{}/db".format(PORT_API))

    if not os.path.exists("res/{}/forum".format(PORT_API)):
        os.makedirs("res/{}/forum".format(PORT_API))

    cfg = {
        "server_name" : "TouchFish",
        "port_api" : PORT_API,
        "port_tcp" : PORT_TCP,
        "email_activate" : "",
        "captcha" : False,
        "email_password" : "",
        "smtp_host" : "",
        "smtp_port" : 465,
        "smtp_use_ssl" : True,
        "reverse_proxy_enabled" : False,
        "proxy_count" : 1,
        "file_last_time" : 72,
        "groups_limit" : 30,
        "single_group_max_people" : 200,
        "rate_limits" : {
            "default" : {"requests": 60, "range": 60}
        },
        "max_file_size" : -1,
        "user_storage_quota" : -1
    }
    write_json("res/{}/config.json".format(PORT_API), cfg)
    write_json("res/{}/captcha/captcha.json".format(PORT_API), {})
    write_json("res/{}/activate.json".format(PORT_API), {})
    write_json("res/{}/forum/queue.json".format(PORT_API), {"queue_num": 0})
    write_json("res/{}/forum/comments.json".format(PORT_API), {})
    write_json("res/{}/announcement.json".format(PORT_API), {})

    avatar.init(PORT_API)
    USER_CURSOR = db.UserDb(HASHER, "res/{}/db/user.db".format(PORT_API), PORT_API, PORT_TCP)
    USER_CURSOR.create_user_table()
    USER_CURSOR.create_friend_table()
    print("[INFO] user.db 创建完毕！")
    FORUM_CURSOR = db.ForumDb('res/{}/db/forum.db'.format(PORT_API), PORT_API, PORT_TCP)
    FORUM_CURSOR.create_forum_table()
    print("[INFO] forum.db 创建完毕！")
    init(PORT_API)
    print("[INFO] file.db 创建完毕！")
    NOTIFICATION_CURSOR = db.NotificationsDb("res/{}/db/notification.db".format(PORT_API), PORT_API)
    print("[INFO] notification.db 创建完毕")
    GROUP_CURSOR = db.GroupDb("res/{}/db/group.db".format(PORT_API), PORT_API)
    GROUP_CURSOR.create_group_table()
    print("[INFO] group.db 创建完毕")
    

    root_username = input("输入 root 用户的用户名：")
    root_password = input("输入 root 用户的密码：")
    USER_CURSOR.user_create(root_username, root_password, time.time())
    USER_CURSOR.change_auth(0, "root")
    NOTIFICATION_CURSOR.create_user_table(0)

    advanced_setup(cfg)

def _ask_bool(prompt, default=False):
    """询问 y/n，回车使用默认值"""
    while True:
        ans = input("{}（y/n，默认 {}）：".format(prompt, "y" if default else "n")).strip().lower()
        if ans == "":
            return default
        if ans in ("y", "yes"):
            return True
        if ans in ("n", "no"):
            return False
        print("请输入 y 或 n。")

def _ask_int(prompt, default):
    """询问整数，回车或输入无效时使用默认值"""
    raw = input("{}（默认 {}）：".format(prompt, default)).strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        print("输入无效，使用默认值 {}。".format(default))
        return default

def advanced_setup(cfg):
    """
    初始化完成后的高级配置：邮箱验证服务（SMTP）、反向代理、数据库后端
    """
    prt("服务器初始化完成！", "green")
    if not _ask_bool("是否进行高级配置？", False):
        prt("未进行高级配置，可稍后在服务器设置中修改。", "yellow")
        return
    changed = False

    if _ask_bool("是否配置邮箱验证服务？", False):
        cfg["smtp_host"] = input("SMTP 服务器地址（留空按邮箱域名自动检测，如 smtp.gmail.com）：").strip()
        cfg["smtp_port"] = _ask_int("SMTP 端口（465=SSL 直连，587=STARTTLS）", cfg["smtp_port"])
        cfg["smtp_use_ssl"] = _ask_bool("使用 SSL 直连？(y=SSL，n=STARTTLS)", True)
        cfg["email_activate"] = input("发件邮箱地址：").strip()
        cfg["email_password"] = input("邮箱密码或授权码：").strip()
        changed = True

    if _ask_bool("服务器是否运行在反向代理（如 Nginx）后方？", False):
        cfg["reverse_proxy_enabled"] = True
        cfg["proxy_count"] = _ask_int("信任的代理层数", cfg["proxy_count"])
        changed = True

    if _ask_bool("是否配置数据库后端？（默认使用 SQLite）", False):
        print("可用的数据库后端：")
        print("  1. SQLite（默认，单文件，推荐内网部署）")
        print("  2. MySQL（适合中等规模，需先安装 MySQL 8.0+ 服务器）[EXPERIMENTAL 实验性]")
        print("  3. PostgreSQL（适合大规模，需先安装 PostgreSQL 服务器）[EXPERIMENTAL 实验性]")
        print("注意：MySQL/PostgreSQL 目前为实验性支持，不保证稳定性。可能造成数据丢失/崩溃，请谨慎使用！")
        choice = input("请选择 [1/2/3]（默认 1）：").strip()
        if choice == "2":
            cfg["db_backend"] = "mysql"
            cfg["db_host"] = input("MySQL 主机地址 [localhost]：").strip() or "localhost"
            cfg["db_port"] = _ask_int("MySQL 端口", 3306)
            cfg["db_user"] = input("MySQL 用户名：").strip()
            cfg["db_password"] = input("MySQL 密码：").strip()
            cfg["db_name"] = input("MySQL 数据库名 [touchfish_v5]：").strip() or "touchfish_v5"
            changed = True
        elif choice == "3":
            cfg["db_backend"] = "postgresql"
            cfg["db_host"] = input("PostgreSQL 主机地址 [localhost]：").strip() or "localhost"
            cfg["db_port"] = _ask_int("PostgreSQL 端口", 5432)
            cfg["db_user"] = input("PostgreSQL 用户名：").strip()
            cfg["db_password"] = input("PostgreSQL 密码：").strip()
            cfg["db_name"] = input("PostgreSQL 数据库名 [touchfish_v5]：").strip() or "touchfish_v5"
            changed = True
        else:
            cfg["db_backend"] = "sqlite"

    if changed:
        write_json("res/{}/config.json".format(PORT_API), cfg)
        prt("高级配置已保存到 res/{}/config.json".format(PORT_API), "green")
    else:
        prt("未进行高级配置，可稍后在服务器设置中修改。", "yellow")

def flask_thread():
    if USE_DEV_SERVER:
        FLASK_APP.run(host='0.0.0.0', port=PORT_API, debug=ENABLE_DEBUG, use_reloader=False, threaded=True)
    else:
        from waitress import serve as waitress_serve
        waitress_serve(FLASK_APP, host='0.0.0.0', port=PORT_API,
                       threads=WAITRESS_THREADS, connection_limit=WAITRESS_CONNECTION_LIMIT)

def tcp_thread():
    asyncio.run(INSTANT_CONTACT.main())

def main(args=None):
    global IMGCAPTCHA
    global FLASK_THREAD
    global INSTANT_CONTACT
    global PORT_API
    global PORT_TCP
    global PUB_KEY
    global HASHER
    global PRI_KEY
    global USER_CURSOR
    global FLASK_APP
    global ENABLE_DEBUG
    global ENABLE_WERKZEUG_LOG
    global USE_DEV_SERVER
    global WAITRESS_THREADS
    global WAITRESS_CONNECTION_LIMIT
    global FORUM_CURSOR
    global FILE_CURSOR
    global NOTIFICATION_CURSOR
    global GROUP_CURSOR
    global MESSAGES_CURSOR
    

    if args is None:
        args = parse_args([])

    normalize_working_directory()
    IMGCAPTCHA = ImageCaptcha()
    HASHER = PasswordHasher(
        time_cost=2,
        memory_cost=65536,
        parallelism=2,
        hash_len=24,
        salt_len=16
    )
    ENABLE_DEBUG = args.debug
    ENABLE_WERKZEUG_LOG = args.log
    USE_DEV_SERVER = args.dev_server
    WAITRESS_THREADS = args.waitress_threads
    WAITRESS_CONNECTION_LIMIT = args.waitress_connection_limit
    print(ASCII_LOGO)
    prt("欢迎来到 TouchFish V5 服务器！", "green")
    chosen = None
    server_lst = None
    cnt = 0
    try:
        with open("server_config.json", "r+") as file:
            server_lst = json.load(file)
        cnt = len(server_lst.keys())
        for keys in server_lst.keys():
            print("[{}] API_PORT: {}, TCP_PORT : {}".format(keys, server_lst[keys][0], server_lst[keys][1]))
        print("[{}] 创建新的服务器".format(cnt))
    except:
        prt("未找到初始服务器配置！", "red")
        create_new_server()
        chosen = -1
    
    if chosen is None:
        if args.create_new_config:
            create_new_server()
            chosen = -1
        elif args.use_config is not None:
            chosen = str(args.use_config)
            if chosen not in server_lst:
                raise ValueError("配置 {} 不存在，请检查 --use-config 参数。".format(chosen))
            PORT_API, PORT_TCP = server_lst[chosen]
        elif args.cli_mode:
            raise ValueError("命令行模式下请使用 --use-config 指定配置，或使用 --create-new-config 创建新配置。")

    if chosen is None:
        chosen = input("选择配置：")
        if chosen == str(cnt):
            create_new_server()
        else:
            PORT_API, PORT_TCP = server_lst[chosen]

    PORT_API = int(PORT_API)
    PORT_TCP = int(PORT_TCP)
    if not (0 <= PORT_API <= 65535 and 0 <= PORT_TCP <= 65535):
        prt("端口不符合要求！", "red")
        sys.exit(1)
    print("服务器在 PORT_API={}, PORT_TCP={} 上部署。".format(PORT_API, PORT_TCP))

    PUB_KEY = load_pub("res/{}/secret/pub.pem".format(PORT_API))
    PRI_KEY = load_pri("res/{}/secret/pri.pem".format(PORT_API))
    pub_pem = open("res/{}/secret/pub.pem".format(PORT_API), "rb")

    config_path = "res/{}/config.json".format(PORT_API)
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            server_cfg = json.load(f)
    except Exception:
        server_cfg = {}

    db_backend = server_cfg.get("db_backend", "sqlite")
    if db_backend in ("mysql", "postgresql"):
        missing = [k for k in ("db_user", "db_password") if not server_cfg.get(k)]
        if missing:
            prt("数据库配置缺失 {}，已回退到 SQLite。".format("、".join(missing)), "yellow")
            db_backend = "sqlite"
    if db_backend == "mysql":
        dialect = MySQLDialect()
        db_dsn = {
            "host": server_cfg.get("db_host", "localhost"),
            "port": server_cfg.get("db_port", 3306),
            "user": server_cfg["db_user"],
            "password": server_cfg["db_password"],
            "database": server_cfg.get("db_name", "touchfish_v5"),
        }
    elif db_backend == "postgresql":
        dialect = PostgreSQLDialect()
        db_dsn = {
            "host": server_cfg.get("db_host", "localhost"),
            "port": server_cfg.get("db_port", 5432),
            "user": server_cfg["db_user"],
            "password": server_cfg["db_password"],
            "database": server_cfg.get("db_name", "touchfish_v5"),
        }
    else:
        dialect = SQLiteDialect()
        db_dsn = None

    if db_dsn is not None:
        USER_CURSOR = db.UserDb(HASHER, db_dsn, PORT_API, PORT_TCP, dialect=dialect)
        USER_CURSOR.create_user_table()
        USER_CURSOR.create_friend_table()
        FORUM_CURSOR = db.ForumDb(db_dsn, PORT_API, PORT_TCP, dialect=dialect)
        FORUM_CURSOR.create_forum_table()
        FILE_CURSOR = db.FileDb(db_dsn, PORT_API, dialect=dialect)
        FILE_CURSOR.create_file_db()
        STICKER_CURSOR = db.StickerDb(db_dsn, PORT_API, dialect=dialect)
        NOTIFICATION_CURSOR = db.NotificationsDb(db_dsn, PORT_API, dialect=dialect)
        MESSAGES_CURSOR = db.MessagesDb(db_dsn, PORT_API, dialect=dialect)
        GROUP_CURSOR = db.GroupDb(db_dsn, PORT_API, dialect=dialect)
    else:
        USER_CURSOR = db.UserDb(HASHER, "res/{}/db/user.db".format(PORT_API), PORT_API, PORT_TCP)
        FORUM_CURSOR = db.ForumDb("res/{}/db/forum.db".format(PORT_API), PORT_API, PORT_TCP)
        FORUM_CURSOR.create_forum_table()
        FILE_CURSOR = db.FileDb("res/{}/file/file.db".format(PORT_API), PORT_API)
        FILE_CURSOR.create_file_db()
        STICKER_CURSOR = db.StickerDb("res/{}/db/sticker.db".format(PORT_API), PORT_API)
        NOTIFICATION_CURSOR = db.NotificationsDb("res/{}/db/notification.db".format(PORT_API), PORT_API)
        MESSAGES_CURSOR = db.MessagesDb("res/{}/db/messages.db".format(PORT_API), PORT_API)
        GROUP_CURSOR = db.GroupDb("res/{}/db/group.db".format(PORT_API), PORT_API)
    if db_dsn is not None and USER_CURSOR.count_users() == 0:
        prt("外部数据库中尚无用户，请创建 root 账户。", "yellow")
        try:
            root_username = input("输入 root 用户的用户名：")
            root_password = input("输入 root 用户的密码：")
            USER_CURSOR.user_create(root_username, root_password, time.time())
            USER_CURSOR.change_auth(0, "root")
            NOTIFICATION_CURSOR.create_user_table(0)
            prt("root 账户已创建。", "green")
        except Exception as e:
            prt("root 账户创建失败: {}".format(e), "red")

    FILE_CURSOR.reconcile_references(
        MESSAGES_CURSOR.get_file_reference_rows() +
        FORUM_CURSOR.get_file_reference_rows()
    )
    def file_auto_collecter():
        last_config_read = 0.0
        expiry = 72
        config_ttl = 300
        while True:
            try:
                now = time.time()
                if now - last_config_read > config_ttl:
                    with open("res/{}/config.json".format(PORT_API), "r", encoding="utf-8") as handle:
                        expiry = json.load(handle).get("file_last_time", 72)
                    last_config_read = now
                collect_expired(PORT_API, STICKER_CURSOR, FILE_CURSOR, expiry)
            except Exception as error:
                print("[WARN] 文件回收失败: {}".format(error))
            time.sleep(60)
    threading.Thread(target=file_auto_collecter, name="tfv5-file-gc", daemon=True).start()
    INSTANT_CONTACT = InstantConnect(
        PORT_API, PORT_TCP, NOTIFICATION_CURSOR, USER_CURSOR,
        MESSAGES_CURSOR, GROUP_CURSOR, FILE_CURSOR,
    )
    FLASK_APP = web.main(PORT_API, PORT_TCP, pub_pem, PRI_KEY, IMGCAPTCHA, USER_CURSOR, FORUM_CURSOR, FILE_CURSOR, NOTIFICATION_CURSOR, MESSAGES_CURSOR, GROUP_CURSOR, INSTANT_CONTACT, STICKER_CURSOR)
    start_api = args.start_api
    if not args.cli_mode:
        prt("注意：生产环境内不适合显式启动 api 服务器！", "yellow")
        stat = input("是否直接显式启动 api 服务器？[y/N]:")
        start_api = (stat == 'Y' or stat == 'y')

    if start_api:
        if USE_DEV_SERVER:
            prt("使用 Flask 开发服务器（仅适用于调试）", "yellow")
            log = logging.getLogger("werkzeug")
            log.disabled = not ENABLE_WERKZEUG_LOG
        else:
            prt("使用内置高性能 waitress 服务器", "green")
        FLASK_THREAD = threading.Thread(target=flask_thread, name="tfv5-api-server")
    print("启动 TCP 服务器")


if __name__ == '__main__':
    try:
        main(parse_args())
        if FLASK_THREAD:
            FLASK_THREAD.start()
        if INSTANT_CONTACT:
            # TCP_THREAD = threading.Thread(target=tcp_thread, name="tfv5-tcp-server")
            # TCP_THREAD.start()
            asyncio.run(INSTANT_CONTACT.main())
    except Exception as e:
        for name in ('FILE_CURSOR', 'USER_CURSOR', 'FORUM_CURSOR', 'GROUP_CURSOR',
                      'NOTIFICATION_CURSOR', 'MESSAGES_CURSOR'):
            obj = globals().get(name)
            if obj is not None:
                try: obj.conn.close()
                except Exception: pass
        prt(e, "red")
        prt("运行或操作错误，程序终止", "red")
        exit()
