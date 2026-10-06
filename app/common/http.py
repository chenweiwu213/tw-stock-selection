"""
HTTP 請求共用模組

處理部份網站憑證缺少 Subject/Authority Key Identifier 擴充欄位，導致 Python 3.13+
預設的嚴格 X.509 驗證誤判成驗證失敗('Missing Subject Key Identifier')的問題
"""

import ssl
import requests
from requests.adapters import HTTPAdapter


class _RelaxedSSLAdapter(HTTPAdapter):
    """
    只關閉過度嚴格的 VERIFY_X509_STRICT 旗標，
    憑證鏈、效期、主機名稱等其他驗證仍正常進行，不是整個關掉驗證。
    """
    def init_poolmanager(self, *args, **kwargs):
        ctx = ssl.create_default_context()
        ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT
        kwargs["ssl_context"] = ctx
        return super().init_poolmanager(*args, **kwargs)


def get_relaxed_session() -> requests.Session:
    session = requests.Session()
    session.mount("https://", _RelaxedSSLAdapter())
    return session