#define UNICODE
#define _UNICODE
#include <windows.h>
#include <shobjidl.h>
#include <shlguid.h>
#include <cstdio>
#include <vector>

// This bridge is called from the editor's message thread, within our own host.
static BOOL CALLBACK own_editor(HWND w, LPARAM out) {
    DWORD pid = 0;
    GetWindowThreadProcessId(w, &pid);
    wchar_t title[128] = {};
    GetWindowTextW(w, title, 128);
    if (pid == GetCurrentProcessId() && IsWindowVisible(w) && wcscmp(title, L"Pedalboard") == 0)
        *reinterpret_cast<HWND*>(out) = w;
    return TRUE;
}
static BOOL CALLBACK child(HWND w, LPARAM result) {
    reinterpret_cast<std::vector<HWND>*>(result)->push_back(w);
    return TRUE;
}
extern "C" __declspec(dllexport) int load_file(const wchar_t* filename) {
    HWND root = nullptr;
    EnumThreadWindows(GetCurrentThreadId(), own_editor, reinterpret_cast<LPARAM>(&root));
    if (!root) return 1;
    IShellItem* item = nullptr;
    HRESULT hr = SHCreateItemFromParsingName(filename, nullptr, IID_PPV_ARGS(&item));
    if (FAILED(hr)) return 2;
    IDataObject* data = nullptr;
    hr = item->BindToHandler(nullptr, BHID_DataObject, IID_PPV_ARGS(&data));
    item->Release();
    if (FAILED(hr)) return 3;
    std::vector<HWND> windows;
    EnumChildWindows(root, child, reinterpret_cast<LPARAM>(&windows));
    windows.push_back(root);
    int result = 4;
    for (HWND w : windows) {
        DWORD pid = 0;
        if (GetWindowThreadProcessId(w, &pid) != GetCurrentThreadId() || pid != GetCurrentProcessId() || !IsWindowVisible(w)) continue;
        auto* target = reinterpret_cast<IDropTarget*>(GetPropW(w, L"OleDropTargetInterface"));
        RECT r = {};
        if (!target || !GetWindowRect(w, &r) || r.right-r.left < 200 || r.bottom-r.top < 100) continue;
        target->AddRef();
        POINTL pt = {r.left+(r.right-r.left)*2/3, r.top+(r.bottom-r.top)/2};
        DWORD effect = DROPEFFECT_COPY;
        hr = target->DragEnter(data, MK_LBUTTON, pt, &effect);
        if (SUCCEEDED(hr)) {
            if (effect != DROPEFFECT_NONE) {
                effect = DROPEFFECT_COPY;
                hr = target->Drop(data, 0, pt, &effect);
                result = SUCCEEDED(hr) ? 0 : 6;
            } else {
                target->DragLeave(); result = 5;
            }
        }
        target->Release();
        if (result == 0) break;
    }
    data->Release();
    return result;
}


