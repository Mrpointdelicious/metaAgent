"""
创建日期：2026-11-18
文件功能：AI诊室（AI问诊）真实链路移植件 —— 移植自 Dify「AI诊室(测试)」工作流代码节点。
"""

import difflib
import json
import re
import unicodedata

# ==================== 场景跳转白名单配置（完整版） ====================
SCENE_JUMP_CONFIG = {
    # ==================== 一楼点位 ====================
    "first_floor": {
        "keywords": [
            "一楼", "1楼", "一楼挂号大厅", "挂号大厅", "大厅",
            "走廊"
        ],
        "action_words": ["去", "到", "前往", "进入", "传送", "带我去", "导航到", "跳转到"],
        "target_prefix": "[ScenePoint:]",
        "target_ids": {
            "一楼": "0", "1楼": "0", "一楼挂号大厅": "0", "挂号大厅": "0", "大厅": "0",
            "走廊": "1"
        }
    },
    "first_floor_exhibition": {
        "keywords": [
            "展示区1", "展示区2", "展示区3", "展示区4", "展示区5",
            "展示区一", "展示区二", "展示区三", "展示区四", "展示区五"
        ],
        "action_words": ["去", "到", "前往", "进入", "传送", "带我去", "导航到", "跳转到"],
        "target_prefix": "[ScenePoint:]",
        "target_ids": {
            "展示区1": "2", "展示区一": "2",
            "展示区2": "3", "展示区二": "3",
            "展示区3": "4", "展示区三": "4",
            "展示区4": "5", "展示区四": "5",
            "展示区5": "6", "展示区五": "6"
        }
    },
    # ==================== 二楼点位 ====================
    "second_floor": {
        "keywords": [
            "二楼", "2楼", "二楼问诊中心", "问诊中心"
        ],
        "action_words": ["去", "到", "前往", "进入", "传送", "带我去", "导航到", "跳转到"],
        "target_prefix": "[ScenePoint:]",
        "target_ids": {
            "二楼": "7", "2楼": "7", "二楼问诊中心": "7", "问诊中心": "7"
        }
    },
    "second_floor_consultation_area": {
        "keywords": [
            "问诊区1", "问诊区2", "问诊区3", "问诊区4", "问诊区5",
            "问诊区一", "问诊区二", "问诊区三", "问诊区四", "问诊区五"
        ],
        "action_words": ["去", "到", "前往", "进入", "传送", "带我去", "导航到", "跳转到"],
        "target_prefix": "[ScenePoint:]",
        "target_ids": {
            "问诊区1": "8", "问诊区一": "8",
            "问诊区2": "9", "问诊区二": "9",
            "问诊区3": "10", "问诊区三": "10",
            "问诊区4": "11", "问诊区四": "11",
            "问诊区5": "12", "问诊区五": "12"
        }
    },
    "second_floor_consultation_room": {
        "keywords": [
            "1号问诊室", "2号问诊室", "3号问诊室", "4号问诊室",
            "5号问诊室", "6号问诊室", "7号问诊室", "8号问诊室",
            "一号问诊室", "二号问诊室", "三号问诊室", "四号问诊室",
            "五号问诊室", "六号问诊室", "七号问诊室", "八号问诊室"
        ],
        "action_words": ["去", "到", "前往", "进入", "传送", "带我去", "导航到", "跳转到"],
        "target_prefix": "[ScenePoint:]",
        "target_ids": {
            "1号问诊室": "13", "一号问诊室": "13",
            "2号问诊室": "14", "二号问诊室": "14",
            "3号问诊室": "15", "三号问诊室": "15",
            "4号问诊室": "16", "四号问诊室": "16",
            "5号问诊室": "17", "五号问诊室": "17",
            "6号问诊室": "18", "六号问诊室": "18",
            "7号问诊室": "19", "七号问诊室": "19",
            "8号问诊室": "20", "八号问诊室": "20"
        }
    },
    # ==================== 三楼点位 ====================
    "third_floor": {
        "keywords": [
            "三楼", "3楼", "三楼诊疗中心", "诊疗中心"
        ],
        "action_words": ["去", "到", "前往", "进入", "传送", "带我去", "导航到", "跳转到"],
        "target_prefix": "[ScenePoint:]",
        "target_ids": {
            "三楼": "21", "3楼": "21", "三楼诊疗中心": "21", "诊疗中心": "21"
        }
    },
    "third_floor_treatment_area": {
        "keywords": [
            "诊疗区1", "诊疗区2", "诊疗区3", "诊疗区4", "诊疗区5",
            "诊疗区一", "诊疗区二", "诊疗区三", "诊疗区四", "诊疗区五"
        ],
        "action_words": ["去", "到", "前往", "进入", "传送", "带我去", "导航到", "跳转到"],
        "target_prefix": "[ScenePoint:]",
        "target_ids": {
            "诊疗区1": "22", "诊疗区一": "22",
            "诊疗区2": "23", "诊疗区二": "23",
            "诊疗区3": "24", "诊疗区三": "24",
            "诊疗区4": "25", "诊疗区四": "25",
            "诊疗区5": "26", "诊疗区五": "26"
        }
    },
    "third_floor_training_room": {
        "keywords": [
            "1号训练室", "2号训练室", "3号训练室", "4号训练室",
            "5号训练室", "6号训练室", "7号训练室", "8号训练室",
            "一号训练室", "二号训练室", "三号训练室", "四号训练室",
            "五号训练室", "六号训练室", "七号训练室", "八号训练室"
        ],
        "action_words": ["去", "到", "前往", "进入", "传送", "带我去", "导航到", "跳转到"],
        "target_prefix": "[ScenePoint:]",
        "target_ids": {
            "1号训练室": "27", "一号训练室": "27",
            "2号训练室": "28", "二号训练室": "28",
            "3号训练室": "29", "三号训练室": "29",
            "4号训练室": "30", "四号训练室": "30",
            "5号训练室": "31", "五号训练室": "31",
            "6号训练室": "32", "六号训练室": "32",
            "7号训练室": "33", "七号训练室": "33",
            "8号训练室": "34", "八号训练室": "34"
        }
    },
    # ==================== 四楼点位 ====================
    "fourth_floor": {
        "keywords": [
            "四楼", "4楼", "四楼评估中心", "评估中心"
        ],
        "action_words": ["去", "到", "前往", "进入", "传送", "带我去", "导航到", "跳转到"],
        "target_prefix": "[ScenePoint:]",
        "target_ids": {
            "四楼": "35", "4楼": "35", "四楼评估中心": "35", "评估中心": "35"
        }
    },
    "fourth_floor_evaluation_area": {
        "keywords": [
            "评估区1", "评估区2", "评估区3", "评估区4", "评估区5",
            "评估区一", "评估区二", "评估区三", "评估区四", "评估区五"
        ],
        "action_words": ["去", "到", "前往", "进入", "传送", "带我去", "导航到", "跳转到"],
        "target_prefix": "[ScenePoint:]",
        "target_ids": {
            "评估区1": "36", "评估区一": "36",
            "评估区2": "37", "评估区二": "37",
            "评估区3": "38", "评估区三": "38",
            "评估区4": "39", "评估区四": "39",
            "评估区5": "40", "评估区五": "40"
        }
    },
    "fourth_floor_evaluation_room": {
        "keywords": [
            "1号评估室", "2号评估室", "3号评估室", "4号评估室",
            "5号评估室", "6号评估室", "7号评估室", "8号评估室",
            "一号评估室", "二号评估室", "三号评估室", "四号评估室",
            "五号评估室", "六号评估室", "七号评估室", "八号评估室",
            "运动评估室", "感觉评估室", "认知评估室", "吞咽评估室",
            "情绪评估室", "言语评估室", "其他评估室"
        ],
        "action_words": ["去", "到", "前往", "进入", "传送", "带我去", "导航到", "跳转到"],
        "target_prefix": "[ScenePoint:]",
        "target_ids": {
            "1号评估室": "41", "一号评估室": "41", "运动评估室": "41",
            "2号评估室": "42", "二号评估室": "42", "感觉评估室": "42",
            "3号评估室": "43", "三号评估室": "43", "认知评估室": "43",
            "4号评估室": "44", "四号评估室": "44",
            "5号评估室": "45", "五号评估室": "45", "吞咽评估室": "45",
            "6号评估室": "46", "六号评估室": "46", "情绪评估室": "46",
            "7号评估室": "47", "七号评估室": "47", "言语评估室": "47",
            "8号评估室": "48", "八号评估室": "48", "其他评估室": "48"
        }
    },
    # ==================== 五楼点位 ====================
    "fifth_floor": {
        "keywords": [
            "五楼", "5楼", "五楼数据中心", "数据中心"
        ],
        "action_words": ["去", "到", "前往", "进入", "传送", "带我去", "导航到", "跳转到"],
        "target_prefix": "[ScenePoint:]",
        "target_ids": {
            "五楼": "49", "5楼": "49", "五楼数据中心": "49", "数据中心": "49"
        }
    },
    "fifth_floor_leisure": {
        "keywords": [
            "休闲区1", "休闲区2", "休闲区3", "休闲区4", "休闲区5",
            "休闲区一", "休闲区二", "休闲区三", "休闲区四", "休闲区五"
        ],
        "action_words": ["去", "到", "前往", "进入", "传送", "带我去", "导航到", "跳转到"],
        "target_prefix": "[ScenePoint:]",
        "target_ids": {
            "休闲区1": "50", "休闲区一": "50",
            "休闲区2": "51", "休闲区二": "51",
            "休闲区3": "52", "休闲区三": "52",
            "休闲区4": "53", "休闲区四": "53",
            "休闲区5": "54", "休闲区五": "54"
        }
    },
    # ==================== AI诊室 ====================
    "ai_clinic": {
        "keywords": [
            "AI诊室", "ai诊室", "智能诊室", "人工智能诊室"
        ],
        "action_words": ["去", "到", "前往", "进入", "传送", "带我去", "导航到", "跳转到"],
        "target_prefix": "[ScenePoint:]",
        "target_ids": {
            "AI诊室": "55", "ai诊室": "55", "智能诊室": "55"
        }
    },
    # ==================== 小游戏/游戏传送 ====================
    "game_jump": {
        "keywords": [
            "小游戏", "游戏", "记忆速配", "饥饿的小鸡", "舒尔特方格", 
            "辨色象限", "太空漫步", "太空漫步(科研版)"
        ],
        "action_words": ["去", "到", "前往", "进入", "玩", "开始", "打开", "启动"],
        "target_prefix": "[GamePoint:]",
        "target_ids": {
            "记忆速配": "0", "饥饿的小鸡": "1", "舒尔特方格": "2",
            "辨色象限": "3", "太空漫步": "4", "太空漫步(科研版)": "5"
        }
    },
    # ==================== 界面传送（社交岛、医院等） ====================
    "interface_jump": {
        "keywords": [
            "社交岛", "会议中心", "上海大学", "岳阳医院", "嘉兴路社区医院",
            "杨浦区社区医院", "第二康复医院", "上海城建职业学院", "华山医院",
            "同济医院", "龙湾养老院", "金海悦心养老院"
        ],
        "action_words": ["去", "到", "前往", "进入", "传送", "返回", "跳转", "打开", "带我去"],
        "target_prefix": "[Telepor:]",
        "target_ids": {
            "社交岛": "0", "会议中心": "1", "上海大学": "2", "岳阳医院": "3",
            "嘉兴路社区医院": "4", "杨浦区社区医院": "5", "第二康复医院": "6",
            "上海城建职业学院": "7", "华山医院": "8", "同济医院": "9",
            "龙湾养老院": "10", "金海悦心养老院": "3"
        }
    },
    # ==================== 病历/报告/记录查看 ====================
    "medical_records": {
        "keywords": [
            "病历", "病历信息", "电子病历", "就诊记录",
            "问诊记录", "问诊历史", "历史问诊",
            "训练报告", "训练记录", "康复报告",
            "患者列表", "病人列表", "患者信息"
        ],
        "action_words": ["打开", "查看", "看", "查", "查询", "显示", "展示", "调出", "给我看", "看看"],
        "target_prefix": "[ClickPoint:]",
        "target_ids": {
            "病历": "14", "病历信息": "14", "电子病历": "14",
            "问诊记录": "10", "问诊历史": "10",
            "训练报告": "12", "康复报告": "12",
            "患者列表": "15", "病人列表": "15", "患者信息": "15"
        }
    },
    # ==================== 摄像头/设备操作 ====================
    "camera_device": {
        "keywords": [
            "摄像头", "相机", "拍照", "录像", "拍摄", "摄像"
        ],
        "action_words": ["打开", "开启", "启动", "关掉", "关闭", "停止", "查看", "看"],
        "target_prefix": "[ClickPoint:]",
        "target_ids": {
            "摄像头": "0", "相机": "0", "拍照": "0", "录像": "0"
        }
    },
    # ==================== 动作指令 ====================
    "action_commands": {
        "keywords": [
            "跳", "跳跃", "跳一下", "跳一跳",
            "跑", "跑步", "加速跑", "快跑", "冲刺",
            "走", "走路", "散步", "漫步",
            "停", "停止", "停下", "暂停",
            "转身", "回头", "向左转", "向右转"
        ],
        "action_words": ["", "要", "想", "帮我", "请", "给我"],
        "target_prefix": "[ActionPoint:]",
        "target_ids": {
            "跳": "2", "跳跃": "2", "跳一下": "2", "跳一跳": "2",
            "跑": "3", "跑步": "3", "加速跑": "3", "快跑": "3", "冲刺": "3",
            "走": "4", "走路": "4", "散步": "4", "漫步": "4",
            "停": "5", "停止": "5", "停下": "5", "暂停": "5",
            "转身": "6", "回头": "6", "向左转": "6", "向右转": "6"
        }
    },
    # ==================== 按键/界面点击 ====================
    "click_jump": {
        "keywords": [
            "挂号", "个人中心", "界面传送", "楼层传送",
            "快速通道", "设备管理", "投屏", "结束问诊", "邀请医生", 
            "填写医嘱", "就诊纪要"
        ],
        "action_words": ["点击", "打开", "进入", "查看", "填写", "投屏", "结束", "邀请"],
        "target_prefix": "[ClickPoint:]",
        "target_ids": {
            "挂号": "0", "个人中心": "1", "界面传送": "3", "楼层传送": "4",
            "快速通道": "5", "设备管理": "6", "投屏": "7", "结束问诊": "8",
            "邀请医生": "9", "填写医嘱": "11", "就诊纪要": "13"
        }
    }
}

# ==================== 场景跳转检测函数 ====================
def detect_scene_jump(user_input: str) -> dict:
    """
    检测用户输入是否匹配场景跳转规则
    返回: { "matched": bool, "target": str, "matched_keyword": str, "scene": str }
    """
    if not user_input:
        return {"matched": False, "target": None, "matched_keyword": None, "scene": None}
    
    s_norm = unicodedata.normalize("NFKC", user_input.strip()).lower()
    
    for scene_name, config in SCENE_JUMP_CONFIG.items():
        # 检查是否包含动作词
        if "" in config["action_words"]:
            has_action = True
        else:
            has_action = any(action in s_norm for action in config["action_words"] if action)
        
        if not has_action:
            continue
        
        # 按关键词长度降序排序，优先匹配更长的关键词
        sorted_keywords = sorted(config["keywords"], key=len, reverse=True)
        matched_keyword = None
        for keyword in sorted_keywords:
            if keyword.lower() in s_norm:
                matched_keyword = keyword
                break
        
        if not matched_keyword:
            continue
        
        # 获取目标ID
        target_id = config["target_ids"].get(matched_keyword)
        if not target_id:
            # 尝试更宽松的匹配
            for kw, tid in config["target_ids"].items():
                if kw.lower() in s_norm:
                    target_id = tid
                    matched_keyword = kw
                    break
        
        if not target_id:
            continue
        
        return {
            "matched": True,
            "target": f"{config['target_prefix']}{target_id}",
            "matched_keyword": matched_keyword,
            "scene": scene_name
        }
    
    return {"matched": False, "target": None, "matched_keyword": None, "scene": None}

# ================= 工具函数和正则表达式（保持不变） =================
def norm(s: str) -> str:
    if not s:
        return ""
    s = unicodedata.normalize("NFKC", str(s))
    s = re.sub(r"[\u200B-\u200D\uFEFF]", "", s)
    s = s.strip().lower()
    s = re.sub(r"[\"'“”‘’「」『』（）\(\)\[\]【】<>〈〉]", "", s)
    s = re.sub(r"[，,]+$", "", s)
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"[。！!？?、…：:]+$", "", s)
    return s

def to_list(x):
    if not x: return []
    if isinstance(x, list): return [str(i) for i in x]
    if isinstance(x, str):
        s = x.strip()
        try:
            arr = json.loads(s)
            if isinstance(arr, list):
                return [str(i) for i in arr]
        except Exception:
            pass
        parts = re.split(r"[\n,，]+", s)
        return [p.strip() for p in parts if p.strip()]
    return [str(x)]

def seq_ratio(a, b):
    return difflib.SequenceMatcher(None, a, b).ratio()

def _is_non_empty_array(x):
    if isinstance(x, (list, tuple)):
        return len(x) > 0
    if isinstance(x, str):
        s = x.strip()
        if not s: return False
        try:
            arr = json.loads(s)
            return isinstance(arr, list) and len(arr) > 0
        except:
            return False
    return False

RE_SELECTION_PHRASE = re.compile(
    r"^(?:我?\s*(?:要|选|选择)?\s*(第?[零〇一二两三四五六七八九十百]{1,6}|[0-9]{1,3})(?:个|项|条|名|号)?)(?:报告)?$"
)
OBJ_REP = r"(?:报告|report|步态|步行)"
RE_REP_WL = re.compile(rf"(?:查|查询|找|搜索|搜|筛|筛选|检索|查看)?\s*(?:[一-龥a-z0-9]{{0,6}})?{OBJ_REP}\s*(?:查|查询|找|搜索)?$")

def white_report(q: str) -> bool:
    s = norm(q)
    return RE_REP_WL.fullmatch(s) is not None

def obj_x_search(q, obj_pattern):
    return re.search(obj_pattern, q) and re.search(r"(?:查|查询|找|搜索|搜|筛|筛选|检索|查看|打开|列出|列表)", q)

_CN_DIGITS = {"零":0,"〇":0,"一":1,"二":2,"两":2,"三":3,"四":4,"五":5,"六":6,"七":7,"八":8,"九":9}
def cn_num_to_int(s: str) -> int:
    s = (s or "").strip()
    if not s: return -1
    if "百" in s:
        left, right = s.split("百", 1)
        h = _CN_DIGITS.get(left, 1) if left else 1
        tail = cn_num_to_int(right) if right else 0
        return h*100 + (0 if tail < 0 else tail)
    if "十" in s:
        left, right = s.split("十", 1)
        t = _CN_DIGITS.get(left, 1) if left else 1
        u = _CN_DIGITS.get(right, 0) if right else 0
        return t*10 + u
    return _CN_DIGITS.get(s, -1)

def parse_selection_index(text: str):
    s = unicodedata.normalize("NFKC", str(text or "")).strip().lower()
    s = re.sub(r"\s+", "", s)
    m = re.fullmatch(r"(?:我?(?:要|选|选择)?第?(\d{1,3})(?:个|项|条|名|号)?(?:报告)?)", s)
    if m: return int(m.group(1))
    m = re.fullmatch(r"(?:我?(?:要|选|选择)?第?([零〇一二两三四五六七八九十百]{1,6})(?:个|项|条|名|号)?(?:报告)?)", s)
    if m:
        val = cn_num_to_int(m.group(1))
        return val if val > 0 else None
    return None

def emit(intent, reason="", conf=0.8, sel_target="", sel_mode="", sel_value=""):
    return {
        "intent": intent,
        "selection_target": sel_target,
        "selection_mode": sel_mode,
        "selection_value": sel_value,
        "confidence": f"{float(conf):.2f}",
        "reason": reason
    }

# ================== 明确非问诊意图判定 ===================
CLEAR_NON_CONSULTATION_INTENTS = {
    "天气": re.compile(r"(?:天气|气温|温度|下雨|晴天|阴天|刮风|空气质量|pm2.5)"),
    "时间": re.compile(r"(?:现在几点|几点了|今天几号|星期几|日期|时间)"),
    "计算": re.compile(r"(?:计算|等于|多少)(?:\d+\s*[+\-*/]\s*\d+|加减乘除)"),
    "翻译": re.compile(r"(?:翻译|怎么说|英文怎么说|用英语怎么说)"),
    "新闻": re.compile(r"(?:新闻|资讯|头条|报道|快讯)"),
    "音乐": re.compile(r"(?:放歌|听歌|音乐|歌曲|播放|暂停|下一首)"),
    "视频": re.compile(r"(?:看视频|看电影|电视剧|综艺|播放视频)"),
    "设置": re.compile(r"(?:设置|配置|调整|修改|更改)(?:音量|亮度|壁纸|主题|密码)"),
    "帮助": re.compile(r"(?:帮助|帮助文档|使用说明|怎么用|功能介绍|客服)"),
    "关于": re.compile(r"(?:关于|介绍|你是谁|开发者|版本|更新)"),
}

NON_HEALTH_KEYWORDS = re.compile(
    r"(?:股票|基金|理财|投资|房价|汽车|手机|电脑|游戏|电影|电视剧|"
    r"体育|足球|篮球|比赛|明星|八卦|娱乐|政治|经济|教育|考试|"
    r"旅游|美食|菜谱|购物|淘宝|京东|快递|物流)"
)

NON_CONSULTATION_PATTERNS = [
    re.compile(r"健康[了过好]|身体[了好棒]"),
    re.compile(r"感谢|谢谢|多谢|感恩"),
    re.compile(r"再见|拜拜|下次聊|结束|退出"),
    re.compile(r"测试|试一下|试试"),
]

# ================== 多轮AI问诊意图判定 ===================
SYMPTOM_KEYWORDS = re.compile(
    r"(?:头疼|头痛|发烧|发热|咳嗽|感冒|流鼻涕|鼻塞|喉咙痛|嗓子痛|咽痛|"
    r"胃痛|肚子痛|腹痛|腹泻|拉肚子|呕吐|恶心|头晕|眩晕|乏力|疲劳|没劲|"
    r"失眠|睡不着|心慌|心悸|胸闷|气短|呼吸困难|皮疹|过敏|红肿|痒|"
    r"酸痛|疼痛|不舒服|难受|不适|症状|感觉|怎么样了|生病|得病|犯病|"
    r"麻|麻木|抽筋|痉挛|胀|肿|痛|疼|晕|喘|咳|吐|烧|"
    r"脚麻|手麻|腿麻|脸麻|发麻|麻麻的)"
)

CONDITION_INQUIRY = re.compile(
    r"(?:怎么回事|什么原因|为什么|怎么办|怎么治|如何治疗|吃什么药|"
    r"严重吗|要紧吗|危险吗|会不会|能好吗|多久能好|需要去医院吗|"
    r"是什么病|什么情况|什么症状|怎么缓解|怎么减轻|怎么处理|"
    r"有.*风险|后遗症|并发症|注意事项)"
)

MEDICAL_HISTORY = re.compile(
    r"(?:以前|之前|过去|历史|有.*吗|吃过.*药|用过.*药|"
    r"过敏史|遗传|家族|慢性病|高血压|糖尿病|心脏病|肝病|肾病)"
)

BODY_PARTS = re.compile(
    r"(?:头|脑袋|眼睛|耳朵|鼻子|嘴巴|喉咙|嗓子|脖子|肩膀|背部|腰|"
    r"肚子|胃|腹部|胸部|心脏|手臂|手|胳膊|腿|膝盖|脚|皮肤|全身|关节|肌肉|"
    r"脚|手|腿|臂|脸|头|颈|肩|背|腰|腹|胸)"
)

SEVERITY_DESCRIPTION = re.compile(
    r"(?:有点|稍微|轻微|比较|很|非常|特别|极其|越来越|"
    r"持续|间断|一阵阵|一直|反复|偶尔|经常|总是|突然|忽然)"
)

MEDICAL_TREATMENT = re.compile(
    r"(?:吃药|用药|打针|输液|手术|检查|化验|拍片|CT|核磁|B超|"
    r"血压|血糖|体温|血常规|尿常规|体检|复查)"
)

def _is_clearly_non_consultation(text):
    if not text or len(text.strip()) < 2:
        return False
    s_norm = unicodedata.normalize("NFKC", text.strip()).lower()
    if NON_HEALTH_KEYWORDS.search(s_norm):
        return True
    for pattern in NON_CONSULTATION_PATTERNS:
        if pattern.search(s_norm):
            return True
    for intent_name, pattern in CLEAR_NON_CONSULTATION_INTENTS.items():
        if pattern.search(s_norm):
            return True
    return False

def _has_symptom_description(text):
    if not text:
        return False
    if SYMPTOM_KEYWORDS.search(text):
        return True
    single_char_symptoms = ["麻", "痛", "疼", "晕", "咳", "吐", "烧", "肿", "胀", "痒"]
    for sym in single_char_symptoms:
        if sym in text:
            return True
    return False

def _has_condition_inquiry(text):
    return bool(CONDITION_INQUIRY.search(text))

def _has_body_part_mention(text):
    if not text:
        return False
    if BODY_PARTS.search(text):
        return True
    single_char_parts = ["头", "手", "脚", "腿", "脸", "腰", "背", "腹", "胸", "颈"]
    for part in single_char_parts:
        if part in text:
            return True
    return False

def _has_medical_history_inquiry(text):
    return bool(MEDICAL_HISTORY.search(text))

def _has_severity_description(text):
    return bool(SEVERITY_DESCRIPTION.search(text))

def _has_medical_treatment_mention(text):
    return bool(MEDICAL_TREATMENT.search(text))


def _in_consultation_flow(conversation_history):
    """上一轮是否为 AI 问诊意图（对齐 Dify conversation 变量里的问诊态）。"""
    if not conversation_history or not isinstance(conversation_history, list):
        return False
    last = conversation_history[-1]
    return isinstance(last, dict) and last.get("intent") == "ai_consultation"


def _is_new_topic(s_norm):
    """明确的新话题（天气/新闻/股票/翻译等）——应切换出问诊流程。"""
    if NON_HEALTH_KEYWORDS.search(s_norm):
        return True
    for pattern in CLEAR_NON_CONSULTATION_INTENTS.values():
        if pattern.search(s_norm):
            return True
    return False


def _is_transition_phrase(s_norm):
    """过渡语（谢谢/再见/测试等）——问诊流程中不代表离开问诊。"""
    for pattern in NON_CONSULTATION_PATTERNS:
        if pattern.search(s_norm):
            return True
    return False


def _is_consultation_continuation(s_norm):
    """问诊流程中可续聊的短句：过渡语 / 无实义省略句。

    粘性只施加于这类句子。带明确问句词或动作意图的句子（「irego 怎么开机」
    「我要挂号」）不算续聊，须放行给产品问答 / 挂号分支，否则会被误粘在
    问诊里，反而回不到知识库。
    """
    if _is_transition_phrase(s_norm):
        return True
    # 明确提问词（怎么办/什么病等医疗问句仍算问诊续聊，其余是新问题）
    if re.search(r"(?:怎么|如何|什么|为什么|哪里|哪儿|能不能|是否)", s_norm):
        return bool(_has_condition_inquiry(s_norm) or _has_symptom_description(s_norm))
    # 短省略回答（「嗯」「一周了」「第二个」）
    return len(s_norm) <= 4


def _detect_ai_consultation(q_raw, conversation_history=None):
    s = q_raw.strip()
    if not s:
        return None
    s_norm = unicodedata.normalize("NFKC", s).lower()

    in_consultation_flow = _in_consultation_flow(conversation_history)

    # 相比原版「一命中非问诊词就 return None」，这里区分两类：
    #   - 明确新话题：始终切换出问诊（用户真的改问别的了）；
    #   - 过渡语：只有不在问诊流程时才拦截，问诊中的一句「谢谢/好的」
    #     不应把意图打回 qa、进而让后续省略句全部落到知识库。
    if _is_new_topic(s_norm):
        return None
    if _is_transition_phrase(s_norm) and not in_consultation_flow:
        return None

    score = 0
    features = []
    has_symptom = _has_symptom_description(s_norm)
    has_inquiry = _has_condition_inquiry(s_norm)
    has_body_part = _has_body_part_mention(s_norm)
    has_history = _has_medical_history_inquiry(s_norm)
    has_severity = _has_severity_description(s_norm)
    has_treatment = _has_medical_treatment_mention(s_norm)
    if has_symptom:
        score += 0.6
        features.append("症状描述")
    if has_inquiry:
        score += 0.4
        features.append("病情询问")
    if has_body_part:
        score += 0.4
        features.append("身体部位")
    if has_history:
        score += 0.3
        features.append("病史咨询")
    if has_treatment:
        score += 0.3
        features.append("医疗治疗")
    if has_severity:
        score += 0.2
        features.append("程度描述")
    if has_symptom and has_body_part:
        score += 0.3
        features.append("症状+部位")
    if has_symptom and has_inquiry:
        score += 0.2
        features.append("症状+询问")
    char_count = len(s_norm)
    if char_count <= 4:
        if has_symptom and has_body_part:
            score += 0.3
            features.append("短文本症状组合")
        elif has_symptom:
            score += 0.2
            features.append("短文本症状")
    if in_consultation_flow:
        # 粘性仅对续聊短句生效；明确的新提问/动作放行，交由后续挂号/QA 分支处理。
        if not _is_consultation_continuation(s_norm) and score < 0.5:
            return None
        score += 0.3
        features.append("问诊流程中")
    if score >= 0.5:
        feature_desc = " + ".join(features) if features else "健康咨询"
        confidence = min(0.98, 0.65 + score * 0.3)
        return emit("ai_consultation", f"检测到AI问诊意图: {feature_desc}", confidence)
    elif score >= 0.3 and (in_consultation_flow or has_symptom):
        return emit("ai_consultation", f"疑似问诊: {', '.join(features)}", 0.7)
    return None

HEALTH_CONSULTATION_PHRASES = [
    "我生病了", "我不舒服", "我难受", "身体不适", "感觉不太好",
    "帮我看看", "咨询一下", "问个问题", "健康咨询", "医疗咨询",
    "我怎么了", "我这是怎么了", "什么病", "怎么回事",
    "脚麻", "手麻", "头疼", "头痛", "发烧", "感冒", "咳嗽",
    "肚子疼", "胃疼", "腰疼", "腿疼"
]

def _quick_match_health_consultation(text):
    text_lower = text.lower()
    for phrase in HEALTH_CONSULTATION_PHRASES:
        if phrase in text_lower:
            return True
    common_symptoms = ["麻", "痛", "疼", "晕", "咳", "烧", "痒", "肿", "胀"]
    common_parts = ["头", "手", "脚", "腿", "腰", "背", "肚", "胃"]
    for sym in common_symptoms:
        for part in common_parts:
            if part + sym in text_lower or sym + part in text_lower:
                return True
    return False

# ================== 医疗动作意图判定 ===================
RE_MEDICAL_ACTIONS = re.compile(r"(?:挂号|看病|看医生|问诊|就诊|治疗|远程问诊|视频问诊|在线问诊|咨询医生)")
RE_MEDICAL_TARGETS = re.compile(r"(?:AI医生|真人医生|医生|专家|门诊|医院|专科|儿科|内科|外科|皮肤科|骨科|牙科)")
RE_IMPERATIVE_MARKERS = re.compile(r"(?:帮我|请帮|我要|我想|想要|需要|帮我预约|请帮我)")
RE_QUESTION_LIKE = re.compile(r"[?？]|(?:吗|么|呢|请问|能否|是否|怎样|怎么|如何)")

def _is_question_like(s):
    return bool(RE_QUESTION_LIKE.search(s or ""))

def _detect_visit_or_medical(q_raw):
    s = q_raw.strip()
    s_norm = unicodedata.normalize("NFKC", s).lower()
    if not _is_clearly_non_consultation(s):
        if _has_symptom_description(s_norm) or _has_condition_inquiry(s_norm):
            return None
    is_question = _is_question_like(s_norm)
    has_imperative = bool(RE_IMPERATIVE_MARKERS.search(s_norm))
    med_match = RE_MEDICAL_ACTIONS.search(s_norm)
    if med_match and (not is_question or has_imperative):
        action = med_match.group(0)
        return emit("medical_action_request", f"检测到医疗动作类请求: {action}", 0.96, sel_target="medical", sel_mode="action", sel_value=action)
    return None

# ================== 主意图判定入口 ===================


def classify_intent(query: str, conversation_history=None) -> dict:
    """意图识别入口（对齐 Dify main(**kwargs)）。

    返回 emit() 结构：intent / selection_target / selection_mode /
    selection_value / confidence / reason。
    """
    q_raw = query or ""
    if not q_raw:
        return emit("qa", "空输入", 0.5)

    scene_result = detect_scene_jump(q_raw)
    if scene_result["matched"]:
        return emit(
            "visit_or_medical_request",
            f"场景跳转: {scene_result['matched_keyword']} -> {scene_result['target']}",
            0.95,
            sel_target="jump",
            sel_mode="scene",
            sel_value=scene_result["target"],
        )

    ai_consultation = _detect_ai_consultation(q_raw, conversation_history)
    if ai_consultation:
        return ai_consultation

    if _quick_match_health_consultation(q_raw):
        return emit("ai_consultation", "快速匹配健康咨询短语", 0.85)

    early_visit_med = _detect_visit_or_medical(q_raw)
    if early_visit_med:
        return early_visit_med

    s_norm = unicodedata.normalize("NFKC", q_raw.strip()).lower()
    for intent_name, pattern in CLEAR_NON_CONSULTATION_INTENTS.items():
        if pattern.search(s_norm):
            return emit(intent_name, f"检测到{intent_name}意图", 0.85)

    return emit("qa", "未命中任何规则→QA", 0.75)
