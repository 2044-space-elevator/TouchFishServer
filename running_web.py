from gevent import monkey

monkey.patch_all()
from web import main
from channel import InstantConnect
from db import *
from captcha.image import ImageCaptcha
from crypto import load_pri
from argon2 import PasswordHasher
PORT_API = 7001
PORT_TCP = 21474
HASHER = PasswordHasher(
        time_cost=1,
        memory_cost=4096,
        parallelism=2,
        hash_len=24,
        salt_len=16
)
usercur = UserDb(HASHER, "res/{}/db/user.db".format(PORT_API), PORT_API, PORT_TCP)
forumcur = ForumDb("res/{}/db/forum.db".format(PORT_API), PORT_API, PORT_TCP)
notificur = NotificationsDb("res/{}/db/notification.db".format(PORT_API), PORT_API)
msgcur = MessagesDb("res/{}/db/messages.db".format(PORT_API), PORT_API)
filecur = FileDb("res/{}/file/file.db".format(PORT_API), PORT_API)
grpcur = GroupDb("res/{}/db/group.db".format(PORT_API), PORT_API)
instcur = InstantConnect(PORT_API, PORT_TCP, notificur, usercur, msgcur, grpcur, filecur)
stickcur = StickerDb("res/{}/db/sticker.db".format(PORT_API), PORT_API)

# 请将 PORT_API, PORT_TCP 换成自己的

pub_pem = open("res/{}/secret/pub.pem".format(PORT_API), "rb")
PRI_KEY = load_pri("res/{}/secret/pri.pem".format(PORT_API))
app = main(PORT_API, PORT_TCP, pub_pem, PRI_KEY, ImageCaptcha(), usercur, forumcur, filecur, notificur, msgcur, grpcur, instcur, stickcur)