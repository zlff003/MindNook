from PIL import Image, ImageDraw, ImageFont
from pathlib import Path


OUT = Path(r"D:\Projects\Agent\MindBridge\MindNook\MindNook智能体总体框架-精简版.png")
W, H = 1800, 1300
S = 2
FONT_BOOST = 1.24


def sc(v):
    if isinstance(v, tuple):
        return tuple(int(x * S) for x in v)
    return int(v * S)


img = Image.new("RGB", sc((W, H)), "#F7F9FC")
d = ImageDraw.Draw(img)

FONT = r"C:\Windows\Fonts\msyh.ttc"
BOLD = r"C:\Windows\Fonts\msyhbd.ttc"


def font(size, bold=False):
    return ImageFont.truetype(BOLD if bold else FONT, sc(size * FONT_BOOST))


def round_rect(box, fill, outline="#CBD5E1", radius=10, width=1):
    d.rounded_rectangle(sc(box), radius=sc(radius), fill=fill, outline=outline, width=sc(width))


def center_text(box, title, subtitle=None, title_size=23, sub_size=16, color="#0F172A", sub_color="#475569"):
    x1, y1, x2, y2 = box
    if subtitle:
        tf, sf = font(title_size, True), font(sub_size)
        tb = d.textbbox((0, 0), title, font=tf)
        sb = d.textbbox((0, 0), subtitle, font=sf)
        total = (tb[3] - tb[1]) + sc(8) + (sb[3] - sb[1])
        y = sc((y1 + y2) / 2) - total / 2
        d.text((sc((x1 + x2) / 2) - (tb[2] - tb[0]) / 2, y), title, font=tf, fill=color)
        d.text((sc((x1 + x2) / 2) - (sb[2] - sb[0]) / 2, y + (tb[3] - tb[1]) + sc(8)), subtitle, font=sf, fill=sub_color)
    else:
        tf = font(title_size, True)
        tb = d.textbbox((0, 0), title, font=tf)
        d.text((sc((x1 + x2) / 2) - (tb[2] - tb[0]) / 2, sc((y1 + y2) / 2) - (tb[3] - tb[1]) / 2), title, font=tf, fill=color)


def section(box, label, color, fill):
    x1, y1, x2, y2 = box
    round_rect(box, fill=fill, outline=color, radius=14, width=2)
    d.rounded_rectangle(sc((x1, y1, x2, y1 + 34)), radius=sc(12), fill=color)
    d.rectangle(sc((x1, y1 + 22, x2, y1 + 36)), fill=color)
    d.text(sc((x1 + 18, y1 + 6)), label, font=font(16, True), fill="#FFFFFF")


def arrow(start, end, color="#64748B", width=2, dashed=False):
    x1, y1 = sc(start); x2, y2 = sc(end)
    if dashed:
        length = max(abs(x2 - x1), abs(y2 - y1))
        steps = max(1, length // sc(10))
        for i in range(0, steps, 2):
            a = i / steps; b = min(1, (i + 1) / steps)
            d.line((x1 + (x2-x1)*a, y1 + (y2-y1)*a, x1 + (x2-x1)*b, y1 + (y2-y1)*b), fill=color, width=sc(width))
    else:
        d.line((x1, y1, x2, y2), fill=color, width=sc(width))
    import math
    ang = math.atan2(y2-y1, x2-x1)
    size = sc(9)
    pts = [(x2, y2), (x2-size*math.cos(ang-0.55), y2-size*math.sin(ang-0.55)), (x2-size*math.cos(ang+0.55), y2-size*math.sin(ang+0.55))]
    d.polygon(pts, fill=color)


def poly_arrow(points, color="#64748B", width=2):
    for a, b in zip(points, points[1:]):
        arrow(a, b, color, width, dashed=True)


def card(box, title, subtitle, accent, fill="#FFFFFF", title_size=22, sub_size=15):
    round_rect(box, fill=fill, outline=accent, radius=9, width=2)
    center_text(box, title, subtitle, title_size, sub_size)


# Title
d.text(sc((W/2, 22)), "MindNook 智能体总体框架", anchor="ma", font=font(31, True), fill="#10213E")

# Users
section((40, 94, 1760, 200), "用户与调用方", "#334155", "#F1F5F9")
for box, title in [((120, 137, 485, 184), "学生"), ((718, 137, 1083, 184), "管理员 / 咨询师"), ((1316, 137, 1680, 184), "外部 Agent / MCP")]:
    card(box, title, "", "#64748B", title_size=21)

# Access
section((40, 220, 1760, 350), "接入与应用层", "#1261A6", "#EFF6FF")
cards = [((110, 265, 500, 330), "Web 前端", "学生端 · 管理端"), ((705, 265, 1095, 330), "FastAPI", "认证 · REST · SSE"), ((1300, 265, 1690, 330), "ChatService", "仅转发安全成品")]
for box, a, b in cards: card(box, a, b, "#3B82F6", title_size=22, sub_size=15)
arrow((500, 298), (705, 298), "#3B82F6"); arrow((1095, 298), (1300, 298), "#3B82F6")
arrow((302, 184), (302, 265), "#3B82F6"); arrow((900, 184), (900, 265), "#3B82F6")

# Runtime
section((40, 370, 1760, 740), "业务编排与多智能体 Runtime", "#34495E", "#F8FAFC")
card((75, 420, 390, 690), "AgentHarness", "会话 · 脱敏 · 落库\n报告 · 工具计划", "#64748B", title_size=25, sub_size=18)

# Runtime inner panel
round_rect((425, 415, 1470, 700), fill="#F7F5FF", outline="#7C3AED", radius=12, width=2)
d.text(sc((447, 430)), "Agent Runtime", font=font(17, True), fill="#6D28D9")
card((570, 455, 1325, 525), "协调 Agent", "任务派生 · 预算 · 采纳", "#8B5CF6", title_size=24, sub_size=15)
card((530, 540, 1365, 605), "CollaborationBlackboard", "Task · Artifact · Message · Event", "#8B5CF6", title_size=21, sub_size=14)
agent_boxes = [((455, 625, 680, 695), "理解 Agent", "意图 / 主题"), ((705, 625, 930, 695), "安全 Agent", "风险 / 复审"), ((955, 625, 1180, 695), "上下文 Agent", "记忆 / RAG / Skill"), ((1205, 625, 1430, 695), "回复 Agent", "生成 / 修复")]
for box, a, b in agent_boxes: card(box, a, b, "#8B5CF6", title_size=18, sub_size=14)
for x in [568, 818, 1068, 1318]: arrow((x, 625), (x, 605), "#7C3AED")
arrow((390, 550), (425, 550), "#64748B"); arrow((1470, 550), (1515, 550), "#DC2626", dashed=True)

# Rules
card((1515, 420, 1725, 510), "固定安全规则", "风险下限", "#EF4444", fill="#FFF7F7", title_size=19, sub_size=15)
card((1515, 530, 1725, 620), "三级业务规则", "日常 · 支持 · 处置", "#EF4444", fill="#FFF7F7", title_size=19, sub_size=14)
card((1515, 640, 1725, 690), "审查后公开", "", "#EF4444", fill="#FFF7F7", title_size=18)

# Data and support
section((40, 760, 1760, 950), "数据、记忆、知识与 Skill", "#217346", "#F0FDF4")
support_cards = [
    ((75, 810, 390, 920), "MySQL / SQLite", "消息 · 报告 · Trace"),
    ((420, 810, 735, 920), "Redis 分层记忆", "近期消息 · Agent 私有记忆"),
    ((765, 810, 1080, 920), "本地混合 RAG", "Chroma + BM25"),
    ((1110, 810, 1425, 920), "心理场景 Skill", "7 个 Workflow · 状态校验"),
    ((1455, 810, 1725, 920), "模型服务", "Mock / Local / API"),
]
for box, a, b in support_cards: card(box, a, b, "#22C55E", title_size=20, sub_size=14)
arrow((225, 760), (225, 690), "#16A34A", dashed=True)
arrow((578, 760), (578, 695), "#16A34A", dashed=True)
arrow((923, 810), (1068, 695), "#16A34A", dashed=True)
arrow((1268, 810), (1068, 695), "#16A34A", dashed=True)
arrow((1590, 810), (1318, 695), "#7C3AED", dashed=True)

# Backend
section((40, 970, 1760, 1175), "后台任务、MCP 与业务处置", "#B45309", "#FFF7ED")
backend = [
    ((75, 1020, 370, 1138), "持久化 ToolJob", "幂等 · DAG · 重试"),
    ((405, 1020, 700, 1138), "ToolDispatcher", "提交后异步执行"),
    ((735, 1020, 1030, 1138), "FastMCP Server", "外部标准调用"),
    ((1065, 1020, 1360, 1138), "ToolOperations", "统一业务实现"),
    ((1395, 1020, 1725, 1138), "处置结果", "台账 · 个案 · 预警"),
]
for box, a, b in backend: card(box, a, b, "#F97316", title_size=20, sub_size=14)
arrow((370, 1079), (405, 1079), "#EA580C"); arrow((700, 1079), (735, 1079), "#EA580C"); arrow((1030, 1079), (1065, 1079), "#EA580C"); arrow((1360, 1079), (1395, 1079), "#EA580C")
arrow((225, 970), (225, 920), "#EA580C", dashed=True)
poly_arrow(((1498, 184), (1740, 184), (1740, 1000), (1030, 1000), (1030, 1079)), "#EA580C")

# Footer guardrail
round_rect((40, 1200, 1760, 1260), fill="#EEF2F7", outline="#94A3B8", radius=8, width=1)
center_text((40, 1200, 1760, 1260), "全链路保障", "隐私脱敏 · 角色权限 · 安全审查 · 审计追踪 · 故障降级", 16, 13)

# Downsample for crisp antialiasing.
img = img.resize((W, H), Image.Resampling.LANCZOS)
img.save(OUT, quality=95, dpi=(180, 180))
print(OUT, OUT.stat().st_size)
