"""Pure, explicit personal-management parsing before either research router."""

from dataclasses import dataclass
import re
import unicodedata


@dataclass(frozen=True)
class PersonalMemoryCommand:
    action: str
    mention: str | None = None
    code: str | None = None
    message: str | None = None


_COST_NOTICE = PersonalMemoryCommand("notice", code="personal_memory_cost_not_supported", message=(
    "本模块尚未实现持仓记录；后续只记录是否持有、期限和风险偏好，不记录成本价和仓位比例。"))
_PREFERENCE_NOTICE = PersonalMemoryCommand("notice", code="personal_memory_preferences_unavailable", message=(
    "本模块目前只支持自选股管理，尚未实现是否持有、期限和风险偏好的记录。"))
_CLEAR_NOTICE = PersonalMemoryCommand("notice", code="personal_memory_clear_confirmation_required", message=(
    "请在自选股面板点击“清空自选”并确认；这只清空自选，不删除聊天历史。"))
_NEGATED_NOTICE = PersonalMemoryCommand("notice", code="personal_memory_command_negated", message=(
    "未修改自选股。需要操作时，请单独明确说“把平安银行加入自选”或“从自选移除平安银行”。"))
_SEPARATE_NOTICE = PersonalMemoryCommand("notice", code="personal_memory_command_needs_separation", message=(
    "请把自选管理与研究分开，每次只管理一只股票；例如“把平安银行加入自选”。本次未修改自选。"))
_INVALID_NOTICE = PersonalMemoryCommand("notice", code="personal_memory_command_invalid", message=(
    "请单独明确说“把平安银行加入自选”“从自选移除平安银行”或“查看自选”；本次未修改自选。"))

_WRITE = r"(?:加入|添加到|加到|放入|移除|移出|删除|删掉|去掉|清空|清除全部)"
_WATCH = r"(?:我的)?自选(?:股|清单)?"
_PREFIX = re.compile(r"^(?:(?:请|麻烦|帮我|请帮我|我想|我要)\s*)+")
_RESEARCH_QUESTION = re.compile(r"(?:值得|适合|合适|应不应该|应该|要不要|需不需要|有必要|是否|能否|为什么|什么是|如何|怎么|怎样)")
_MULTIPLE = re.compile(r"[,，、;；/|&]|以及|还有|\S{2,}(?:和|与|及)\S{2,}")
_PERSONAL_MANAGE = re.compile(
    r"(?:^|[,，。;；]|然后|并且|并|再)(?:(?:请|麻烦|帮我|我想|我要)\s*)*"
    r"(?:不要|别|不再|停止)?(?:(?:记住|记录|保存|忘记)|(?:更新|修改|设置|删除|清除)(?:一下|下)?(?:我的|我|本人))")


def parse_personal_memory_command(question):
    """Return None for research/knowledge; notices never authorize any write."""
    if type(question) is not str or not question.strip() or len(question) > 2000:
        return None
    text = unicodedata.normalize("NFKC", question).strip()
    if _PERSONAL_MANAGE.search(text) and re.search(r"成本(?:价)?|仓位(?:比例)?", text):
        return _COST_NOTICE
    if _PERSONAL_MANAGE.search(text) and re.search(r"持有|持仓|风险偏好|偏好期限", text):
        return _PREFERENCE_NOTICE
    if "自选" not in text:
        return None
    text = _PREFIX.sub("", text).strip().rstrip("。!！").strip()
    command_anchor = re.search(r"(?:把|将).{1,80}" + _WRITE + _WATCH + r"|从" + _WATCH, text)
    joined_command = re.search(_WRITE + _WATCH + r"(?:并|然后|同时|之后|再)", text)
    list_anchor = re.match(r"(?:查看|显示|列出|列一下|看看)" + _WATCH, text)
    if ((command_anchor or joined_command or list_anchor)
            and re.search(r"(?:并(?:且)?|然后|同时|之后|再).{0,100}(?:研究|分析)"
                          r"|(?:研究|分析).{0,100}(?:然后|并(?:且)?|同时|之后|再)", text)):
        return _SEPARATE_NOTICE
    # Questions about suitability or the concept itself are ordinary research, not commands.
    if _RESEARCH_QUESTION.search(text):
        return None
    if re.fullmatch(r"(?:查看|显示|列出|列一下|看看)" + _WATCH, text):
        return PersonalMemoryCommand("list")
    if (list_anchor and not re.search(_WRITE, text)
            and re.match(r"(?:中|里|内)", text[list_anchor.end():])):
        return None
    action_hint = re.search(_WRITE + r".{0,100}" + _WATCH + r"|" + _WATCH + r".{0,100}" + _WRITE, text)
    if not action_hint and not list_anchor and not re.search(r"(?:查看|显示|列出|列一下)" + _WATCH, text):
        return None
    if re.search(r"不要|别把|别将|别从|别加入|别移除|不(?:要|用|必|需|想|加入|移除|添加)|禁止|不许|没有|未曾", text):
        return _NEGATED_NOTICE
    if re.fullmatch(r"(?:清空|清除全部|删除全部)" + _WATCH, text):
        return _CLEAR_NOTICE
    patterns = (
        ("add", r"(?:把|将)?(?P<mention>.+?)(?:加入|添加到|加到|放入)" + _WATCH),
        ("remove", r"从" + _WATCH + r"(?:中|里)?(?:移除|移出|删除|删掉|去掉)(?P<mention>.+)"),
        ("remove", r"(?:把|将)?(?P<mention>.+?)从" + _WATCH + r"(?:中|里)?(?:移除|移出|删除|删掉|去掉)"),
    )
    for action, pattern in patterns:
        match = re.fullmatch(pattern, text)
        if match:
            mention = match.group("mention").strip()
            if (len(mention) > 80 or not mention.isprintable() or not mention
                    or _MULTIPLE.search(mention) or re.search(_WRITE + r"|研究|分析|然后|并且|同时|之后", mention)):
                return _SEPARATE_NOTICE
            if re.search(r"[?？]|吗$|么$|好吗$", mention):
                return _INVALID_NOTICE
            return PersonalMemoryCommand(action, mention=mention)
    if re.search(r"研究|分析|然后|并且|同时|之后|[,，、;；]|并", text):
        return _SEPARATE_NOTICE
    return _INVALID_NOTICE
