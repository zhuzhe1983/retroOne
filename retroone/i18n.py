"""Small server-owned interface catalog. Never translate library metadata."""
import re


MESSAGES = {
    'zh-CN': {
        'back_library': '返回游戏库',
        'load_server_save': '载入服务器存档',
        'export_server_save': '导出服务器存档',
        'upload_existing_save': '上传已有存档',
        'start_game': '开始游戏',
        'controls_help': 'WASD 移动 · J/K/U/I 动作 · Enter 开始 · Shift 选择 · 键位及 Xbox / PlayStation 手柄可在模拟器菜单调整。游戏内存档保存在当前浏览器；点击模拟器“保存状态”会下载存档并备份到服务器。原作低于 60 帧时保留原速。',
        'save_backed_up': '存档已备份到服务器',
        'save_invalid': '模拟器未生成有效存档',
        'save_downloaded_backup_failed': '文件已下载，服务器备份失败：',
        'save_backup_failed': '备份失败：',
        'save_too_large': '存档不能超过 32 MiB',
        'save_empty': '存档为空',
        'save_not_found': '暂无服务器存档',
        'browser_resources_missing': '浏览器模拟器资源尚未安装',
        'browser_bios_missing': '游戏需要 BIOS；请配置对应固件',
        'interface_settings_unavailable': '界面设置无法读取或保存，请检查配置文件；原有设置未重置',
        'interface_settings_busy': '界面设置正在更新，请稍后重试',
        'invalid_interface_settings': '请选择自动、简体中文或 English',
    },
    'en': {
        'back_library': 'Back to library',
        'load_server_save': 'Load server save',
        'export_server_save': 'Export server save',
        'upload_existing_save': 'Upload existing save',
        'start_game': 'Start game',
        'controls_help': 'WASD: move · J/K/U/I: actions · Enter: start · Shift: select. Configure keys and Xbox / PlayStation controllers in the emulator menu. In-game saves stay in this browser. The emulator’s “Save State” action downloads a save and backs it up to the server. Games originally below 60 FPS keep their original speed.',
        'save_backed_up': 'Save backed up to the server',
        'save_invalid': 'The emulator did not produce a valid save state',
        'save_downloaded_backup_failed': 'File downloaded, but server backup failed: ',
        'save_backup_failed': 'Backup failed: ',
        'save_too_large': 'Save states cannot exceed 32 MiB',
        'save_empty': 'The save state is empty',
        'save_not_found': 'No server save is available yet',
        'browser_resources_missing': 'Browser emulator resources are not installed',
        'browser_bios_missing': 'This game needs a BIOS. Add the required firmware first.',
        'interface_settings_unavailable': 'Interface settings could not be read or saved. Check the configuration file; existing settings have not been reset.',
        'interface_settings_busy': 'Interface settings are being updated. Try again shortly.',
        'invalid_interface_settings': 'Choose Auto, Simplified Chinese or English',
    },
}


def resolve_locale(preference, accept_language=''):
    """Resolve our two languages, respecting q weights and explicit exclusions.

    Regional English and Chinese tags use the corresponding available language.
    Unsupported/invalid headers fall back to Simplified Chinese. The header is
    bounded and interpreted only when the player explicitly selects ``auto``.
    """
    if preference in MESSAGES:
        return preference
    if preference != 'auto' or not isinstance(accept_language, str):
        return 'zh-CN'
    ranges = []
    for index, item in enumerate(accept_language[:8192].split(',')[:64]):
        parts = [part.strip() for part in item.split(';')]
        tag = parts[0].lower()
        if not re.fullmatch(r'\*|[a-z]{1,8}(?:-[a-z0-9]{1,8})*', tag):
            continue
        quality = 1.0
        if len(parts) > 1:
            if len(parts) != 2 or not re.fullmatch(r'q\s*=\s*(?:0(?:\.\d{0,3})?|1(?:\.0{0,3})?|\.\d{1,3})', parts[1], re.I):
                continue
            quality = float(parts[1].split('=', 1)[1].strip())
        ranges.append((tag, quality, index))
    choices = []
    for locale, family in (('zh-CN', 'zh'), ('en', 'en')):
        matches = [item for item in ranges if item[0].split('-')[0] == family]
        if not matches:
            matches = [item for item in ranges if item[0] == '*']
        if matches:
            _, quality, index = max(matches, key=lambda item: (item[1], -item[2]))
            if quality > 0:
                choices.append((quality, -index, locale))
    # Stable max keeps the Chinese default for a tied wildcard preference.
    return max(choices, key=lambda item: item[:2])[2] if choices else 'zh-CN'


def text(locale, key):
    return MESSAGES.get(locale, MESSAGES['zh-CN'])[key]


def player_messages(locale):
    return dict(MESSAGES.get(locale, MESSAGES['zh-CN']))


def emulator_locale(locale):
    # EmulatorJS 4.2.3 names its English catalog en-US, not en.
    return 'en-US' if locale == 'en' else 'zh-CN'
