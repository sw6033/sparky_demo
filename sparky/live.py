"""Negotiate a browser voice session using the server's project credential."""
import json
import ssl
import sys
from pathlib import Path
from urllib import request, error
from .brain import TEACHING_STYLE

MODEL = "gpt-live-1.1"


def connect(key, sdp, conversation=()):
    examples = " ".join(f'{e["cues"][0]} → {e["word"]}（{e["offer"]}）' for e in conversation)
    config = {
        "model": MODEL, "store": False,
        "instructions": (
            "你是 Sparky，一位以英语交流、适时引入中文的双语 AI 伙伴。现在由成人模拟儿童测试。"
            "用一两句短句，每次只问一个问题。允许中英混说，等对方说完。"
            "用孩子平时说的话来邀请他，比如一起画画、去找小猫。孩子只说一个词也可以接着聊。"
            "听到问候就简单回应，例如 Hi! I'm here. 然后等对方开口。"
            "只在对方主动表达兴趣时才开始活动。不要把你提出的建议说成对方已经做出的选择。"
            "课程选择、练习回答、暂停或继续都委托给教学后台，照后台返回的话继续。"
            "只有后台确认后才能说活动已切换或练习已完成。"
            + TEACHING_STYLE + "随聊由你当场回应。以下例句可按孩子的话改写：" + examples
        ),
        "delegation": {"type": "client"},
        "client": {"data_channel": {"allowed_client_events": [
            "session.close", "session.commentary.append", "session.thinking.append"
        ]}},
    }
    payload = {"session": config, "transport": {"type": "webrtc", "sdp": sdp}}
    req = request.Request("https://api.openai.com/v1/live/sessions",
                          data=json.dumps(payload).encode(),
                          headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
    try:
        context = ssl.create_default_context()
        # Some macOS Python distributions ship without an OpenSSL CA bundle.
        if sys.platform == "darwin" and not context.get_ca_certs() and Path("/etc/ssl/cert.pem").is_file():
            context.load_verify_locations(cafile="/etc/ssl/cert.pem")
        with request.urlopen(req, timeout=20, context=context) as response:
            answer = json.load(response)["transport"]["sdp"]
            if not isinstance(answer, str) or not answer.startswith("v=0"):
                raise ValueError()
    except error.HTTPError as exc:
        reasons = {401: "项目密钥无效", 403: "项目没有访问权限", 404: "项目无法使用该模型或接口",
                   429: "项目额度或请求频率受限"}
        return 502, {"error": f"GPT-Live 连接失败（{exc.code}）：{reasons.get(exc.code, '服务拒绝了会话请求')}。"}
    except (OSError, ValueError, KeyError, TypeError):
        return 502, {"error": "未收到 GPT-Live 的有效连接应答。"}
    return 201, {"sdp": answer}
