from pathlib import Path
import math

from PIL import Image, ImageDraw, ImageFont


OUT = Path(r"D:\Projects\Agent\MindBridge\MindNook\MindNook多智能体协作时序图.png")
W, H, S = 1900, 1680, 2
img = Image.new("RGB", (W*S, H*S), "#F8FAFC")
d = ImageDraw.Draw(img)
REG = r"C:\Windows\Fonts\msyh.ttc"
BOLD = r"C:\Windows\Fonts\msyhbd.ttc"


def sc(v):
    if isinstance(v, tuple):
        return tuple(int(x*S) for x in v)
    return int(v*S)


def f(size, bold=False):
    return ImageFont.truetype(BOLD if bold else REG, sc(size))


def rr(box, fill, outline, radius=10, width=2):
    d.rounded_rectangle(sc(box), radius=sc(radius), fill=fill, outline=outline, width=sc(width))


def text_center(box, text, size=20, bold=True, color="#14213D"):
    x1,y1,x2,y2=box
    d.multiline_text(sc(((x1+x2)/2,(y1+y2)/2)), text, anchor="mm", align="center", font=f(size,bold), fill=color, spacing=sc(3))


def dashed_line(p1,p2,color="#94A3B8",width=2,dash=8,gap=8):
    x1,y1=sc(p1); x2,y2=sc(p2)
    dist=math.hypot(x2-x1,y2-y1)
    if not dist:return
    ux,uy=(x2-x1)/dist,(y2-y1)/dist
    pos=0
    while pos<dist:
        end=min(dist,pos+sc(dash))
        d.line((x1+ux*pos,y1+uy*pos,x1+ux*end,y1+uy*end),fill=color,width=sc(width))
        pos+=sc(dash+gap)


def arrow(x1,y,x2,label,color="#2563A6",dashed=False,label_size=17):
    if dashed: dashed_line((x1,y),(x2,y),color,2,7,6)
    else: d.line(sc((x1,y,x2,y)),fill=color,width=sc(2))
    direction=1 if x2>x1 else -1
    d.polygon([sc((x2,y)),sc((x2-12*direction,y-7)),sc((x2-12*direction,y+7))],fill=color)
    d.text(sc(((x1+x2)/2,y-10)),label,anchor="ms",font=f(label_size),fill="#334155")


def note(box,title,lines,fill="#F1F5F9",outline="#94A3B8"):
    rr(box,fill,outline,12,2)
    x1,y1,x2,y2=box
    d.text(sc((x1+18,y1+13)),title,font=f(18,True),fill="#1E293B")
    y=y1+48
    for line in lines:
        d.text(sc((x1+25,y)),line,font=f(16),fill="#334155")
        y+=30


# Title
d.text(sc((W/2,28)),"多智能体协作时序图",anchor="ma",font=f(34,True),fill="#10213E")

names=["AgentHarness","CoordinatorAgent","Collaboration\nBlackboard","UnderstandingAgent","SafetyAgent","ContextAgent","ResponseAgent"]
xs=[115,375,650,930,1205,1480,1750]
colors=["#DDEEFE","#DDEEFE","#E7F5EA","#E8F2FC","#FFF2E4","#E8F2FC","#E8F2FC"]
outlines=["#68A7D3","#68A7D3","#54A36D","#68A7D3","#E58B2A","#68A7D3","#68A7D3"]
for x,n,fill,ol in zip(xs,names,colors,outlines):
    rr((x-100,90,x+100,150),fill,ol,10,2); text_center((x-100,90,x+100,150),n,18,True)
    dashed_line((x,150),(x,1640),"#9AB1C8",2,6,8)

# Phase bands
for y,label,color in [(175,"输入与初始化","#334155"),(430,"分流与上下文","#0F766E"),(900,"生成与安全审查","#7C3AED"),(1320,"采纳、落库与后台任务","#B45309")]:
    rr((20,y,1880,y+36),color,color,8,1); d.text(sc((40,y+6)),label,font=f(16,True),fill="#FFFFFF")

# 1 initialization
arrow(xs[0],240,xs[1],"脱敏消息 + MemoryContext")
arrow(xs[1],295,xs[2],"创建根任务，发布 hard_rule")
note((360,330,1075,405),"固定安全下限",["命中明确危险：发布 SAFETY_OVERRIDE，等级不得降低"],"#FFF8D9","#D4A72C")

# 2 route
arrow(xs[1],485,xs[2],"创建 understand_input")
arrow(xs[3],535,xs[2],"claim + 发布 intent")
note((280,570,1320,705),"三级分流",[
    "日常交流：CoordinatorAgent 直接发布 risk，不调用心理 RAG / Skill",
    "心理支持：SafetyAgent 评估；加载基础支持、转介及主题 Skill",
    "风险处置：SafetyAgent 合并规则下限；加载高风险安全计划与转介 Skill",
],"#EFF6FF","#7FA3C8")
arrow(xs[1],745,xs[2],"心理支持 / 风险处置：创建 assess_risk")
arrow(xs[4],790,xs[2],"claim + 发布 risk / SAFETY_OVERRIDE")
arrow(xs[1],835,xs[2],"创建 collect_context")
arrow(xs[5],875,xs[2],"claim + 发布 context：记忆 + RAG + Skill")

# 3 generation/review
arrow(xs[1],950,xs[2],"创建 prepare_response")
arrow(xs[6],1005,xs[2],"发布 response_prompt / proposal v1")
arrow(xs[6],1060,xs[4],"创建 review_response")
arrow(xs[4],1115,xs[2],"claim + 发布 safety_review v1")
note((310,1150,1770,1295),"审查分支",[
    "通过：CoordinatorAgent 核对 proposal 版本，发布 final_response + FINAL_ADOPTED",
    "拒绝：创建 repair_response → ResponseAgent 生成 v2 → SafetyAgent 再审查",
    "再次拒绝、预算耗尽或关键 Skill 失效：采用固定安全回退",
],"#F6F4FF","#8B78C6")

# 4 persist/public/jobs
arrow(xs[2],1375,xs[1],"最终安全结果",dashed=True)
arrow(xs[1],1425,xs[0],"返回 AgentResult",dashed=True)
note((250,1460,1835,1605),"AgentHarness 提交与公开",[
    "同一最终事务：助手消息 + Trace + 报告 / 交接摘要 + ToolJob",
    "提交成功后公开 SSE；心理支持写台账；风险处置创建个案，个案成功后触发预警",
],"#FFF8E7","#D69E2E")

img=img.resize((W,H),Image.Resampling.LANCZOS)
img.save(OUT,quality=95,dpi=(180,180))
print(OUT,OUT.stat().st_size)
