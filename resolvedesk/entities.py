"""Literal entities with per-field corrections and original character spans.

Normalization maps full-width ASCII one character at a time, preserving source
offsets. Chinese days are parsed only as explicit integer durations; dates,
elapsed incidents, fractions and ranges are not silently treated as a choice.
"""
import re

DIGITS={'零':0,'〇':0,'一':1,'二':2,'两':2,'三':3,'四':4,'五':5,'六':6,'七':7,'八':8,'九':9}
FINANCIAL=str.maketrans('壹贰叁肆伍陆柒捌玖拾佰','一二三四五六七八九十百')
NUMBER='零〇一二两三四五六七八九十百壹贰叁肆伍陆柒捌玖拾佰'
PATTERNS={
    'resource_id':r'(?<![a-z0-9_])(?:repo-[a-z0-9_-]+|docs-[a-z0-9_-]+)(?![a-z0-9_])',
    'asset_id':r'(?<![a-z0-9_])LT-\d{3,8}(?![a-z0-9_])',
    'ticket_id':r'(?<![a-z0-9_])RD-[A-F0-9]{12}(?![a-z0-9_])',
    'access_level':r'(?<![a-z0-9_])(?:read|write|admin)(?![a-z0-9_])|只读|读写|只写',
    'duration_days':r'(?<![0-9.'+NUMBER+r'])[-负]?(?:[0-9]{1,4}|['+NUMBER+r']{1,6})\s*[天日]',
    'error_code':r'(?<![a-z0-9_])(?:401|403|404|500|691|800|809)(?![a-z0-9_])',
    'os':r'(?<![a-z0-9_])(?:Windows\s*(?:10|11)|Ubuntu\s*[\d.]*|macOS\s*[a-z0-9.]*)',
    'software':r'(?<![a-z0-9_])(?:IntelliJ(?:\s+IDEA)?(?:\s+Ultimate)?|IDEA(?:\s+Ultimate)?|VS\s*Code|PyCharm|Python|Docker)(?![a-z0-9_])'}
BOUNDARY=re.compile(r'[，,;；。！？!？\n]')
CORRECTION=re.compile(r'改成|改为|换成|换为|设为|调整为|更正为|更新为')
NEGATIVE=re.compile(r'(?:不是|不要|不用|不选|不再|无需|而非|而不是|别用|别选)(?:使用|申请|选择|采用|原来的|之前的)?\s*$')


def normalize(text):
    return ''.join(chr(ord(c)-0xFEE0) if '\uff01'<=c<='\uff5e' else ' ' if c=='\u3000' else c for c in text)


def chinese_integer(value):
    value=value.translate(FINANCIAL)
    if value in DIGITS:return DIGITS[value]
    if re.fullmatch(r'[一二两三四五六七八九]?十[一二三四五六七八九]?',value):
        left,right=value.split('十');return (DIGITS[left] if left else 1)*10+(DIGITS[right] if right else 0)
    if re.fullmatch(r'[一二两三四五六七八九]百(?:零[一二三四五六七八九]|[一二三四五六七八九]十[一二三四五六七八九]?)?',value):
        rest=value[2:];return DIGITS[value[0]]*100+(chinese_integer(rest.removeprefix('零')) if rest else 0)
    return None


def canonical(field,value):
    if field=='duration_days':
        amount=re.sub(r'\s*[天日]$','',value);negative=amount.startswith(('-','负'));amount=amount.lstrip('-负')
        number=int(amount) if amount.isascii() and amount.isdigit() else chinese_integer(amount)
        return None if number is None else str(-number if negative else number)
    if field=='access_level':return {'只读':'read','读写':'write','只写':'write'}.get(value,value.lower())
    if field in {'asset_id','ticket_id'}:return value.upper()
    return value


def extract(message):
    text=normalize(message);result={};spans={};cleared=[]
    boundaries=[match.start() for match in BOUNDARY.finditer(text)]
    for field,pattern in PATTERNS.items():
        candidates=[];last=None
        for match in re.finditer(pattern,text,re.I):
            start,end=match.span();left=max((p+1 for p in boundaries if p<start),default=0)
            right=min((p for p in boundaries if p>=end),default=len(text))
            before,after=text[left:start],text[end:right]
            kept=bool(re.match(r'\s*(?:先|暂时|暂且)?不要(?:修改|改动|改变|改)',after))
            negated=bool(NEGATIVE.search(before) or not kept and re.match(r'\s*(?:先|暂时|暂且)?(?:不对|错误|不要|不需要|不选|不是我想|不是我要)',after))
            if last and last['negative'] and re.fullmatch(r'\s*(?:或|和|以及|与|、|或者|及)\s*',text[last['end']:start]):negated=True
            value=canonical(field,match.group())
            if value is None:continue
            if field=='duration_days':
                if re.search(r'(?:[0-9'+NUMBER+r']\s*(?:到|至|[-~～])|月)\s*$',before):continue
                if re.match(r'\s*(?:前|后|内|以来|之前|以后|了)',after) and not negated:continue
            candidate={'start':start,'end':end,'text':message[start:end],'value':value,'negative':negated}
            candidates.append(candidate);last=candidate
        positives=[item for item in candidates if not item['negative']]
        # A replacement's new side is authoritative for that field only.
        replacements=list(re.finditer(r'(?:用|使用|采用)([^，,;；。\n]{1,120}?)替换',text))
        for replacement in replacements:
            selected=[item for item in positives if replacement.start(1)<=item['start']<replacement.end(1)]
            if selected:
                clause_end=min((p for p in boundaries if p>replacement.end()),default=len(text))
                positives=selected+[item for item in positives if item['start']>clause_end]
        # Keep every explicit alternative after a correction, never just the
        # last value. Markers in another field's clause cannot erase this field.
        for marker in CORRECTION.finditer(text):
            clause_end=min((p for p in boundaries if p>=marker.end()),default=len(text))
            selected=[item for item in positives if marker.end()<=item['start']<clause_end]
            if selected:positives=selected+[item for item in positives if item['start']>clause_end]
        if positives:
            result[field]=list(dict.fromkeys(item['value'] for item in positives))
            spans[field]=[{k:v for k,v in item.items() if k!='negative'} for item in positives]
        elif any(item['negative'] for item in candidates):cleared.append(field)
    return {'values':result,'spans':spans,'cleared':cleared}
