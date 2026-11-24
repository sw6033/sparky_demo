"""Sentence boundaries outside quoted teaching examples; no text rewriting."""
def cost(text):
    return sum(3 if '\u3400'<=char<='\u9fff' else 1 for char in text)

def split(text):
    parts=[]
    start=0
    quoted=False
    for i,char in enumerate(text):
        if char in '"“”「」':
            quoted=not quoted
        if quoted or char not in '.!?。！？':
            continue
        if char=='.' and i and i+1<len(text) and text[i-1].isdigit() and text[i+1].isdigit():
            continue
        if char in '.!?' and i+1<len(text) and not text[i+1].isspace():
            continue
        part=text[start:i+1].strip()
        if part:
            parts.append(part)
        start=i+1
        if len(parts)==5:
            break
    if text[start:].strip():
        parts.append(text[start:].strip())
    # A standalone "Okay!" is too short to hide preparation of the next sentence.
    # Join complete sentences only; quoted examples are never split or rewritten.
    if cost(text)>160 and len(parts)>1 and cost(parts[0])<=12 and cost(parts[0]+parts[1])<=160:
        parts[:2]=[parts[0]+" "+parts[1]]
    return [{"index":i,"text":part,"delivery":"pending"} for i,part in enumerate(parts)]

def attach(reply):
    if not reply.get("segments"):
        reply["segments"]=split(reply["text"])
    # Small replies avoid a separate connection/validation wait for each sentence.
    # Keep sentence receipts; a whole-clip interruption conservatively marks all
    # sentences interrupted because we have no verified word-level playhead.
    reply["speech_mode"]="whole" if cost(reply["text"])<=160 else "sentences"
    return reply
