import enum


class DestinationStatus(str, enum.Enum):
    PENDING = "待登记"
    CONFIRMED = "已落实"
    CHANGING = "变动中"
    VERIFIED = "已核实"


class DestinationType(str, enum.Enum):
    FURTHER_STUDY = "升学"
    EMPLOYMENT = "就业"
    UNDECIDED = "待落实"


class SalaryRange(str, enum.Enum):
    BELOW_6 = "6万以下"
    RANGE_6_8 = "6-8万"
    RANGE_8_10 = "8-10万"
    RANGE_10_15 = "10-15万"
    ABOVE_15 = "15万以上"


class SalaryChange(str, enum.Enum):
    DECREASED = "下降"
    UNCHANGED = "持平"
    INCREASED_SMALL = "小幅增长"
    INCREASED_LARGE = "大幅增长"


class WarningType(str, enum.Enum):
    CONFIRMED_RATE_DECLINE = "落实率连续下降"
    ALIGNED_RATE_DECLINE = "对口就业率连续下降"
    BELOW_PROVINCE_LINE = "低于全省对照线"


class WarningLevel(str, enum.Enum):
    YELLOW = "黄色预警"
    ORANGE = "橙色预警"
    RED = "红色预警"


class AttributionCategory(str, enum.Enum):
    CURRICULUM = "专业设置"
    INDUSTRY_COOLING = "行业遇冷"
    TRAINING_QUALITY = "培养质量"
    OTHER = "其他因素"


class WarningStatus(str, enum.Enum):
    ACTIVE = "预警中"
    RESOLVED = "已解决"
    DISMISSED = "已忽略"


class DispositionScope(str, enum.Enum):
    COLLEGE_LEADERSHIP = "学院领导"
    TEACHING_OFFICE = "教务部门"
    STUDENT_AFFAIRS = "学工部门"
    CAREER_SERVICE = "就业指导"
    MAJOR_TEAM = "微专业教研组"


INDUSTRIES = [
    "信息技术",
    "金融",
    "制造业",
    "教育",
    "医疗健康",
    "建筑业",
    "零售业",
    "能源",
    "文化传媒",
    "政府机关",
    "其他"
]
