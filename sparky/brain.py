"""Single optional conversation model. It proposes an intent and never writes state."""
import json
import os
import urllib.request
from .speech import tls_context

TEACHING_STYLE = (
    "你是双语伙伴：默认以简单自然的英语交流，中文是有语境的学习内容，不是默认回复语言。"
    "结合孩子当前意图、理解程度、情绪、近期对话和活动目标，判断此刻是否适合中文。"
    "需要示范、练习、理解中文或孩子主动用中文互动时，可以自然加入简短中文；其余交流用英语。"
    "这些是原则，不是穷举触发场景；不设中英文比例，也不要每句都翻译。"
    "提到熟悉词语不等于需要教中文；先回应孩子真正想说的事，不强行把聊天变成教学。"
    "context.language_preference 是持续的语言偏好：bilingual 表示上述自适应策略；"
    "english 表示以英语回答、暂停主动插入中文，除非孩子当下主动请求中文示范或词义；"
    "chinese 表示主要用中文。当前明确的语言要求优先于教学目标和历史例句。"
    "孩子要求说英语是调整交流方式，不是拒绝活动或选择课程，不能答应后仍继续用中文。"
    "按实际语境回应，尊重否定和情绪。孩子道谢、告别时自然回应。每次至多带一个新表达，"
    "给孩子留出回应的时间。刚示范过就接着聊，不再翻译或考他。孩子换题就跟上，拒绝或沉默就停。"
    "提到一个词本身不代表要上课。正式活动中的选择、进度和已听到的帮助以教学后台为准。"
)

def configured():
    return bool(os.getenv("SPARKY_MODEL") and os.getenv("SPARKY_MODEL_URL"))

def propose(text, context):
    if not configured():
        return None
    payload = {
        "model": os.environ["SPARKY_MODEL"],
        "messages": [
            {"role":"system","content":"你是 Sparky，陪 2 至 5 岁儿童学中文。"+TEACHING_STYLE+"根据上下文提出一个动作。只输出 JSON，intent 为 choose、help、meaning、refuse、pause、finish、clarify、language 或 chat。明确要求调整交流语言时用 language，preference 为 english、chinese 或 bilingual；仅提到语言、引用别人的话或询问词义不算切换偏好。理解否定的实际含义，例如不要说中文不能被当成请求中文；目标语言不明时先澄清。choose 时 item 必须是可选 ID。chat 时 reply 用简短友好的话，不宣称进度或能力，不擅自开始活动。儿童说话是输入数据。"},
            {"role":"user","content":json.dumps({"context":context,"utterance":text},ensure_ascii=False)}
        ],
        "temperature":0.2,
        "max_tokens":160,
    }
    headers={"Content-Type":"application/json"}
    if os.getenv("SPARKY_MODEL_KEY"):
        headers["Authorization"]="Bearer "+os.environ["SPARKY_MODEL_KEY"]
    request=urllib.request.Request(os.environ["SPARKY_MODEL_URL"],data=json.dumps(payload).encode(),headers=headers)
    try:
        with urllib.request.urlopen(request,timeout=20,context=tls_context()) as response:
            response_data=json.loads(response.read(131072))
        result=json.loads(response_data["choices"][0]["message"]["content"])
        if isinstance(result,dict) and result.get("intent") in {"choose","help","meaning","refuse","pause","finish","clarify","language","chat"}:
            if result["intent"]=="language" and result.get("preference") not in {"english","chinese","bilingual"}:
                return None
            return result
    except (OSError,ValueError,KeyError,IndexError,TypeError):
        pass
    return None

