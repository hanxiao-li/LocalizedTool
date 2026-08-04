"""生成自签 HTTPS 证书（局域网加密访问用，无需额外安装）。

用项目已有的 cryptography 库生成，证书与私钥写到 data/cert.pem、data/ssl_key.pem
（data/ 已被 git 忽略，不会泄漏）。SAN 自动包含本机局域网 IP，浏览器可"继续访问"。

注意：自签证书浏览器会提示一次"不安全"（因为非公共 CA 签发），点"高级→继续访问"
即可，流量是 TLS 加密的。若本机 IP 变化，重新运行本脚本即可。
"""

import datetime
import ipaddress
import os
import socket
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from config import DATA_DIR, ensure_dirs


def get_lan_ips() -> list[str]:
    """尽力收集本机 IPv4 地址（主出口 + 全部非回环地址）。"""
    ips = set()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('8.8.8.8', 80))
        ips.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith('127.'):
                ips.add(ip)
    except OSError:
        pass
    return sorted(ips)


def gen_cert() -> None:
    ensure_dirs()
    ips = get_lan_ips()

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'LocalizedTool')])

    san = [x509.DNSName('localhost')]
    san.append(x509.IPAddress(ipaddress.ip_address('127.0.0.1')))
    for ip in ips:
        try:
            san.append(x509.IPAddress(ipaddress.ip_address(ip)))
        except ValueError:
            continue

    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.datetime.utcnow() - datetime.timedelta(days=1))
        .not_valid_after(datetime.datetime.utcnow() + datetime.timedelta(days=3650))
        .add_extension(x509.SubjectAlternativeName(san), critical=False)
        .sign(key, hashes.SHA256())
    )

    cert_path = os.path.join(DATA_DIR, 'cert.pem')
    key_path = os.path.join(DATA_DIR, 'ssl_key.pem')
    with open(cert_path, 'wb') as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))
    with open(key_path, 'wb') as f:
        f.write(key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        ))

    print('已生成自签证书:')
    print('  ', cert_path)
    print('  ', key_path)
    print('本机局域网 IP:', ips or '未检测到')
    for ip in ips:
        print(f'局域网加密访问地址: https://{ip}:5000')
    print('注意: 浏览器首次会提示"不安全/继续访问"，点过去即可，流量已加密。')


if __name__ == '__main__':
    gen_cert()
