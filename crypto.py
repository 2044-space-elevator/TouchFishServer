from __future__ import annotations
from cryptography.hazmat.primitives.asymmetric import rsa, padding
from argon2.exceptions import VerifyMismatchError
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives import padding as sym_padding
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat, NoEncryption, PrivateFormat
import hashlib  
import base64
import os
import json
import functools

from flask import request
from jwt_tool import LEGACY_NOTE

def load_pri(path : str):
    """
    返回私钥对象
    """
    with open(path, "rb") as key_file:
        private_key = serialization.load_pem_private_key(
            key_file.read(),
            password=None,
            backend=default_backend()
        )
    return private_key

def load_pub(path : str):
    """
    返回公钥对象
    """
    with open(path, "rb") as key_file:
        public_key = serialization.load_pem_public_key(
            key_file.read(),
            backend=default_backend()
        )
    
    return public_key

def return_app_route(app,  pri, auth_resolver=None):
    """
    app 是一个 flask 对象
    这个装饰器的目的是为了重载 app.route 方法，使其默认支持加密
    两个参数 app 和 key，key 是 cryptography 私钥对象

    auth_resolver: 可选的身份解析回调，签名 resolver(content) -> (ok, result)
        ok=True: result = (identity dict, legacy bool)，identity 至少包含 uid
        ok=False: result = 错误响应（dict 或 str）
    """
    def res(*args, **kwargs):
        """
        和 @app.route 一样，传入 path 与 methods
        """
        def decorator(func):
            @functools.wraps(func)
            def wrapper(*wrapper_args, **wrapper_kwargs):
                req_data = request.get_json()
                try:
                    aes_key, iv_bytes, content = deal_req_data(req_data, pri)
                    content = json.loads(json.dumps(content))
                    if auth_resolver is not None:
                        ok, resolved = auth_resolver(content)
                        if not ok:
                            ret = resolved
                        else:
                            identity, legacy = resolved
                            if identity is not None:
                                content["uid"] = identity["uid"]
                                content.setdefault("password", "")
                            ret = func(content, *wrapper_args, **wrapper_kwargs)
                            if legacy and isinstance(ret, dict) and "token" not in ret:
                                ret = dict(ret)
                                ret["note"] = LEGACY_NOTE
                    else:
                        ret = func(content, *wrapper_args, **wrapper_kwargs)
                    if not isinstance(ret, str):
                        ret = json.dumps(ret)
                    iv, ret = aes_encrypt(ret, aes_key)
                    iv = base64.b64encode(iv).decode("utf-8")
                    ret = base64.b64encode(ret).decode("utf-8")
                    return {"iv" : iv, "content" : ret}
                except Exception as e:
                    print("[ERR] 来自客户端错误访问导致的异常：{}".format(e))
                    aes_key = ""
                    iv_bytes = ""
                    content = {}
                    if not content:
                        return "Wrong Requests!" 
                        
            return app.route(*args, **kwargs)(wrapper)

        return decorator

    return res


def deal_req_data(data : dict, pri):
    """
    data 是所传数据，pri 是加密的
    """
    try:
        orginal_key_bytes = base64.b64decode(data["key"])
        iv_bytes = base64.b64decode(data["iv"])
        content = base64.b64decode(data["content"])
        aes_key = decrypt(pri, orginal_key_bytes)
        content = aes_decrypt(iv_bytes, content, aes_key)
        content = json.loads(content)
        return aes_key, iv_bytes, content
    except Exception as e:
        return {}
    


def sha256(data : str | bytes) -> str:
    if isinstance(data, str):
        data = bytes(data, encoding="utf-8")

    sha256_hash = hashlib.sha256()
    sha256_hash.update(data)

    return sha256_hash.hexdigest()

def aes_encrypt(plain : str | bytes, key : bytes) -> bytes:
    if isinstance(plain, str):
        plain = bytes(plain, encoding="utf-8")
    iv = os.urandom(16)
    cipher = Cipher(
        algorithms.AES(key),
        modes.CBC(iv),
        backend=default_backend()
    )
    padder = sym_padding.PKCS7(128).padder()
    padded_data = padder.update(plain) + padder.finalize()

    encryptor = cipher.encryptor()
    ciphertext = encryptor.update(padded_data) + encryptor.finalize()
    return iv, ciphertext

def aes_decrypt(iv : bytes, ciphertext : str | bytes, key : bytes) -> bytes:
    if isinstance(ciphertext, str):
        ciphertext = bytes(ciphertext, encoding="utf-8")
    cipher = Cipher(
        algorithms.AES(key),
        modes.CBC(iv),
        backend=default_backend()
    )

    decryptor = cipher.decryptor()
    padded_plain = decryptor.update(ciphertext) + decryptor.finalize()

    unpadder = sym_padding.PKCS7(128).unpadder()
    plaintext = unpadder.update(padded_plain) + unpadder.finalize()

    return plaintext

def generate_aes_key():
    return os.urandom(32)

def generate_rsa_keys() -> tuple:
    """
    返回格式：(pri_key, pub_key, pub_pem, pri_pem, hash of pub_pem)
    """
    private_key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=2048
    )

    public_key = private_key.public_key()
    public_pem = public_key.public_bytes(
        encoding=Encoding.PEM,
        format=PublicFormat.SubjectPublicKeyInfo
    )
    private_pem = private_key.private_bytes(
        encoding=Encoding.PEM,
        format=PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=NoEncryption()
    )
    return private_key, public_key, private_pem, public_pem, sha256(public_pem)

def decrypt(pri, secret : str | bytes) -> bytes:
    if isinstance(secret, str):
        secret = bytes(secret, encoding='utf-8') 
    plain = pri.decrypt(
        secret,
        padding.OAEP(
            mgf = padding.MGF1(algorithm=hashes.SHA256()),
            algorithm=hashes.SHA256(),
            label = None
        )
    ) 
    return plain

def encrypt(pub, plain : str | bytes) -> bytes:
    if isinstance(plain, str):
        plain = bytes(plain, encoding='utf-8')
    secret = pub.encrypt(
        plain,
        padding.OAEP(
            mgf=padding.MGF1(algorithm=hashes.SHA256()),
            algorithm=hashes.SHA256(),
            label=None
        )
    )
    return secret

def pwd_verify(hasher, hash_str : str, pwd : str):
    try:
        if hasher.verify(hash_str, pwd):
            return True
    except VerifyMismatchError:
        return False
    return False