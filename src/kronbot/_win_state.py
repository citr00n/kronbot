"""Проверки владельца и DACL пользовательского хранилища Windows."""

import win32api
import win32con
import win32security


def validate(path):
    info = win32security.GetNamedSecurityInfo(
        str(path), win32security.SE_FILE_OBJECT,
        win32security.OWNER_SECURITY_INFORMATION | win32security.DACL_SECURITY_INFORMATION,
    )
    token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
    user = win32security.GetTokenInformation(token, win32security.TokenUser)[0]
    if info.GetSecurityDescriptorOwner() != user:
        raise ValueError("неверный владелец")
    acl = info.GetSecurityDescriptorDacl()
    if acl is None or acl.GetAceCount() == 0:
        raise ValueError("пустой DACL")
    allowed = {win32security.ConvertSidToStringSid(user), "S-1-5-18", "S-1-5-32-544"}
    for index in range(acl.GetAceCount()):
        ace = acl.GetAce(index)
        kind = ace[0][0]
        if kind not in (win32security.ACCESS_ALLOWED_ACE_TYPE, win32security.ACCESS_DENIED_ACE_TYPE):
            raise ValueError("неподдерживаемый ACE")
        if kind == win32security.ACCESS_ALLOWED_ACE_TYPE and win32security.ConvertSidToStringSid(ace[2]) not in allowed:
            raise ValueError("небезопасный DACL")
