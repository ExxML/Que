/*
 * Que's low-level input hooks in native code.
 *
 * Windows passes a key or click on to the active app when a low-level hook callback
 * misses its deadline (LowLevelHooksTimeout), so the callback must never wait. This
 * library runs WH_KEYBOARD_LL and WH_MOUSE_LL on its own thread, at the highest thread
 * priority, with its code, state and stack locked in memory. The callback decides with
 * fixed-size tables: no allocation, no locks, no Python and no blocking calls.
 *
 * Only Que's own shortcut key (Right Ctrl) and modifiers pressed while it is held are held
 * back; every other modifier reaches the active app at once, so normal typing keeps its
 * natural timing and auto-repeat.
 *
 * The decision rules are a line-by-line port of core/hook_engine.py HookEngine, which
 * stays the reference: test/test_native_hook.py feeds both the same input and requires
 * identical decisions, replays and events. The policy tables (hotkeys, modifiers, key
 * classes, system chords) are passed in from Python so they are declared only once.
 *
 * Que's state arrives through a single-producer command ring and is applied on the hook
 * thread; decisions leave through a single-consumer event ring. Neither side ever waits
 * for the other.
 *
 * Build: python native/build.py (writes src/assets/que_hook.dll).
 */

#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <stdint.h>
#include <string.h>

#define QUE_API __declspec(dllexport)

#ifndef _MSC_VER
/* MinGW's headers lack the acquire/release helpers */
#define ReadAcquire64(source) __atomic_load_n((source), __ATOMIC_ACQUIRE)
#define WriteRelease64(destination, value) \
    __atomic_store_n((destination), (value), __ATOMIC_RELEASE)
#endif

#define QUE_MAX_HOTKEYS 64
#define QUE_MAX_REGIONS 32
#define QUE_MAX_CHORDS 32
#define QUE_MAX_MODIFIERS 16
#define QUE_MAX_REPLAYS 32
#define QUE_EVENT_RING 8192 /* Power of two */
#define QUE_COMMAND_RING 64 /* Power of two */
#define QUE_LATE_MS 200

#define PASS 0
#define CONSUME 1

#define CLASS_DEVICE 0x01
#define CLASS_LOCK 0x02
#define CLASS_WINDOWS 0x04

#define LLMHF_INJECTED_FLAG 0x01

enum {
    EVENT_ACTION = 1,
    EVENT_LOCK = 2,
    EVENT_ARMED = 3,
    EVENT_KEY = 4,
    EVENT_POINTER = 5,
    EVENT_QUIT_READY = 6,
    EVENT_OBSERVE_KEY = 7,
    EVENT_OBSERVE_BUTTON = 8,
    EVENT_OBSERVE_WHEEL = 9,
};

enum {
    COMMAND_VISIBLE = 1,
    COMMAND_ARMED = 2,
    COMMAND_ROUTING = 3,
    COMMAND_REGIONS = 4,
    COMMAND_QUIT = 5,
    COMMAND_RELEASE_ALL = 6,
};

enum {
    STATUS_KEYBOARD = 0x01,
    STATUS_MOUSE = 0x02,
    STATUS_MEMORY_LOCKED = 0x04,
    STATUS_PRIORITY = 0x08,
};

#define WM_QUE_WAKE (WM_APP + 1)
#define WM_QUE_REINSTALL (WM_APP + 2)
#define WM_QUE_STOP (WM_APP + 3)

typedef struct {
    int64_t hwnd;
    int32_t left, top, right, bottom;
} QueRegion;

typedef struct {
    int32_t type;
    int32_t value;
    uint32_t seq;
    int32_t count;
    QueRegion regions[QUE_MAX_REGIONS];
} QueCommand;

typedef struct {
    int32_t type;
    int32_t a, b, c;
    uint32_t d;
    uint32_t seq;
    uint32_t held;
    uint32_t tick;
    int64_t hwnd;
} QueEvent;

typedef struct {
    int32_t vk, scan, flags;
} QueReplay;

typedef struct {
    uint32_t status;
    uint32_t keyboard_error;
    uint32_t mouse_error;
    uint32_t late_events;
    uint32_t max_late_ms;
    uint32_t dropped_events;
} QueStats;

typedef struct {
    int32_t mask, vk, action, repeat, always;
} Hotkey;

/* Sends replayed key events; SendInput when live, a recorder that re-enters the engine in tests */
typedef void (*QueSender)(const QueReplay *replays, int count);

typedef struct {
    int32_t button;
    int64_t hwnd;
} OwnedButton;

typedef struct {
    /* Event ring first so the hook thread's writes and Python's reads share no cache line
       with the decision state */
    QueEvent events[QUE_EVENT_RING];
    volatile LONG64 event_head; /* Written by the hook thread */
    volatile LONG64 event_tail; /* Written by the consumer */
    QueCommand commands[QUE_COMMAND_RING];
    volatile LONG64 command_head; /* Written by the producer */
    volatile LONG64 command_tail; /* Written by the hook thread */

    /* Policy, fixed before start */
    Hotkey hotkeys[QUE_MAX_HOTKEYS];
    int hotkey_count;
    uint8_t modifier_bit[256]; /* Non-zero for modifier keys */
    int8_t modifier_index[256]; /* Bit position in event "held" masks, -1 otherwise */
    int modifier_count;
    uint8_t key_class[256];
    int32_t chords[QUE_MAX_CHORDS][2];
    int chord_count;
    int observe;
    int que_key; /* Que's shortcut key (Right Ctrl): withheld with every modifier pressed after it */

    /* Decision state, touched only by the deciding thread */
    int window_visible;
    int input_armed;
    int routing_enabled;
    int quit_pending;
    uint32_t press_seq;
    QueRegion regions[QUE_MAX_REGIONS];
    int region_count;
    uint8_t held_vk[256];
    uint8_t suppressed_vk[256];
    uint8_t input_vk[256];
    uint8_t held_modifier[256];
    int held_vk_count;
    int suppressed_vk_count;
    int held_modifier_count;
    QueReplay buffered[QUE_MAX_MODIFIERS]; /* Withheld modifier presses, in press order */
    int buffered_count;
    uint8_t repeat_present[256];
    int32_t repeat_mask[256];
    int32_t repeat_action[256];
    OwnedButton owned[5]; /* Mouse presses Que owns, in press order */
    int owned_count;
    uint32_t delivered_buttons; /* Bit set of buttons another app received */
    int64_t hover_target;

    /* Key events to replay once the current decision is complete */
    QueReplay replays[QUE_MAX_REPLAYS];
    int replay_count;
    QueSender send;

    /* Shared with other threads */
    volatile LONG drained;
    volatile LONG notify_pending;
    volatile LONG late_events;
    volatile LONG max_late_ms;
    volatile LONG dropped_events;
    volatile LONG status;
    volatile LONG keyboard_error;
    volatile LONG mouse_error;

    HWND notify_window;
    UINT notify_message;
    HANDLE thread;
    DWORD thread_id;
    HANDLE ready;
    HHOOK keyboard_hook;
    HHOOK mouse_hook;
} QueEngine;

static QueEngine *active_engine;

/* ---- Ring buffers ------------------------------------------------------------------- */

static void push_event(QueEngine *e, int32_t type, int32_t a, int32_t b, int32_t c, uint32_t d,
                       uint32_t held, int64_t hwnd, uint32_t tick) {
    LONG64 head = e->event_head;
    if (head - ReadAcquire64(&e->event_tail) >= QUE_EVENT_RING) {
        InterlockedIncrement(&e->dropped_events);
        return;
    }
    QueEvent *slot = &e->events[head & (QUE_EVENT_RING - 1)];
    slot->type = type;
    slot->a = a;
    slot->b = b;
    slot->c = c;
    slot->d = d;
    slot->seq = e->press_seq;
    slot->held = held;
    slot->tick = tick;
    slot->hwnd = hwnd;
    WriteRelease64(&e->event_head, head + 1);
}

/* Return a bit set of the held modifiers, one bit per configured modifier in ascending VK order. */
static uint32_t held_mask(const QueEngine *e) {
    uint32_t mask = 0;
    for (int vk = 0; vk < 256; vk++) {
        if (e->held_modifier[vk] && e->modifier_index[vk] >= 0) {
            mask |= 1u << e->modifier_index[vk];
        }
    }
    return mask;
}

/* ---- Decision rules (port of HookEngine) --------------------------------------------- */

static int is_drained(const QueEngine *e) {
    return !(e->suppressed_vk_count || e->buffered_count || e->held_modifier_count ||
             e->owned_count);
}

static void finish_quit_if_released(QueEngine *e) {
    if (e->quit_pending && is_drained(e)) {
        e->quit_pending = 0;
        push_event(e, EVENT_QUIT_READY, 0, 0, 0, 0, 0, 0, 0);
    }
}

static void set_armed(QueEngine *e, int armed, int notify) {
    if (notify && armed != e->input_armed) {
        push_event(e, EVENT_ARMED, armed, 0, 0, 0, 0, 0, 0);
    }
    e->input_armed = armed;
}

static void request_quit(QueEngine *e) {
    e->quit_pending = 1;
    finish_quit_if_released(e);
}

static void apply_command(QueEngine *e, const QueCommand *command) {
    switch (command->type) {
    case COMMAND_VISIBLE:
        e->window_visible = command->value != 0;
        if (!e->window_visible) {
            set_armed(e, 0, 0);
        }
        break;
    case COMMAND_ARMED:
        /* A response to an older press must not override a newer press's decision */
        if (command->seq >= e->press_seq) {
            set_armed(e, command->value != 0, 0);
        }
        break;
    case COMMAND_ROUTING:
        e->routing_enabled = command->value != 0;
        if (!e->routing_enabled) {
            set_armed(e, 0, 0);
        }
        break;
    case COMMAND_REGIONS: {
        int count = command->count;
        if (count < 0) {
            count = 0;
        }
        if (count > QUE_MAX_REGIONS) {
            count = QUE_MAX_REGIONS;
        }
        memcpy(e->regions, command->regions, sizeof(QueRegion) * (size_t)count);
        e->region_count = count;
        break;
    }
    case COMMAND_QUIT:
        request_quit(e);
        break;
    case COMMAND_RELEASE_ALL:
        /* Claim no new input once Que is gone, but keep owning held presses */
        e->window_visible = 0;
        e->routing_enabled = 0;
        e->region_count = 0;
        set_armed(e, 0, 0);
        break;
    default:
        break;
    }
}

static void apply_commands(QueEngine *e) {
    LONG64 tail = e->command_tail;
    LONG64 head = ReadAcquire64(&e->command_head);
    while (tail != head) {
        apply_command(e, &e->commands[tail & (QUE_COMMAND_RING - 1)]);
        tail++;
        WriteRelease64(&e->command_tail, tail);
    }
}

static int modifier_mask(const QueEngine *e) {
    int mask = 0;
    for (int vk = 0; vk < 256; vk++) {
        if (e->held_modifier[vk]) {
            mask |= e->modifier_bit[vk];
        }
    }
    return mask;
}

static const Hotkey *find_hotkey(const QueEngine *e, int mask, int vk, int always) {
    for (int i = 0; i < e->hotkey_count; i++) {
        const Hotkey *h = &e->hotkeys[i];
        if (h->mask == mask && h->vk == vk && (h->always != 0) == always) {
            return h;
        }
    }
    return NULL;
}

static int is_system_chord(const QueEngine *e, int mask, int vk) {
    for (int i = 0; i < e->chord_count; i++) {
        if (e->chords[i][0] == mask && e->chords[i][1] == vk) {
            return 1;
        }
    }
    return 0;
}

static int windows_key_held(const QueEngine *e) {
    for (int vk = 0; vk < 256; vk++) {
        if (e->held_vk[vk] && (e->key_class[vk] & CLASS_WINDOWS)) {
            return 1;
        }
    }
    return 0;
}

static int buffered_find(const QueEngine *e, int vk) {
    for (int i = 0; i < e->buffered_count; i++) {
        if (e->buffered[i].vk == vk) {
            return i;
        }
    }
    return -1;
}

static void buffered_remove(QueEngine *e, int index) {
    for (int i = index; i + 1 < e->buffered_count; i++) {
        e->buffered[i] = e->buffered[i + 1];
    }
    e->buffered_count--;
}

static void emit_key(QueEngine *e, int vk, int scan, int flags, int down, int repeated) {
    push_event(e, EVENT_KEY, vk, scan, flags, (uint32_t)(down | (repeated << 1)), held_mask(e), 0,
               0);
}

static void invoke(QueEngine *e, int action) {
    push_event(e, EVENT_ACTION, action, 0, 0, 0, 0, 0, 0);
}

static void add_replay(QueEngine *e, int vk, int scan, int flags) {
    if (e->replay_count < QUE_MAX_REPLAYS) {
        QueReplay *r = &e->replays[e->replay_count++];
        r->vk = vk;
        r->scan = scan;
        r->flags = flags;
    }
}

/* Replay a non-Que chord intact: the buffered modifier presses in press order, then the key. */
static void replay_modifiers_and_key(QueEngine *e, int vk, int scan, int flags) {
    for (int i = 0; i < e->buffered_count; i++) {
        const QueReplay *press = &e->buffered[i];
        add_replay(e, press->vk, press->scan,
                   (press->flags & LLKHF_EXTENDED) ? KEYEVENTF_EXTENDEDKEY : 0);
    }
    e->buffered_count = 0;
    add_replay(e, vk, scan, (flags & LLKHF_EXTENDED) ? KEYEVENTF_EXTENDEDKEY : 0);
}

static void set_flag(uint8_t *set, int *count, int vk, int value) {
    if (set[vk] != (value != 0)) {
        set[vk] = (uint8_t)(value != 0);
        *count += value ? 1 : -1;
    }
}

static int process_key(QueEngine *e, int code, WPARAM message, int vk, int scan, int flags,
                       uint32_t tick) {
    if (code < 0 || (message != WM_KEYDOWN && message != WM_SYSKEYDOWN && message != WM_KEYUP &&
                     message != WM_SYSKEYUP)) {
        return PASS;
    }
    if (flags & LLKHF_INJECTED) {
        return PASS;
    }
    vk &= 0xFF;
    int down = message == WM_KEYDOWN || message == WM_SYSKEYDOWN;
    if (e->observe) {
        push_event(e, EVENT_OBSERVE_KEY, vk, scan, (flags & LLKHF_EXTENDED) ? 1 : 0,
                   (uint32_t)down, 0, 0, tick);
    }
    if (e->modifier_bit[vk]) {
        if (down) {
            if (e->held_modifier[vk]) {
                /* Autorepeat follows its press: withheld stays withheld, delivered passes */
                return buffered_find(e, vk) >= 0 ? CONSUME : PASS;
            }
            set_flag(e->held_modifier, &e->held_modifier_count, vk, 1);
            if (vk != e->que_key && buffered_find(e, e->que_key) < 0 && !e->input_armed) {
                return PASS; /* Not part of a Que shortcut or typing: reaches the app at once */
            }
            if (e->buffered_count < QUE_MAX_MODIFIERS) {
                QueReplay *press = &e->buffered[e->buffered_count++];
                press->vk = vk;
                press->scan = scan;
                press->flags = flags;
            }
            return CONSUME;
        }
        set_flag(e->held_modifier, &e->held_modifier_count, vk, 0);
        int index = buffered_find(e, vk);
        if (index >= 0) {
            buffered_remove(e, index);
            finish_quit_if_released(e);
            return CONSUME;
        }
        finish_quit_if_released(e);
        return PASS;
    }
    int repeated = e->held_vk[vk];
    int modifiers = modifier_mask(e);
    if (!down) {
        set_flag(e->held_vk, &e->held_vk_count, vk, 0);
        e->repeat_present[vk] = 0;
        if (e->suppressed_vk[vk]) {
            set_flag(e->suppressed_vk, &e->suppressed_vk_count, vk, 0);
            if (e->input_vk[vk]) {
                e->input_vk[vk] = 0;
                emit_key(e, vk, scan, flags, 0, 0);
            }
            finish_quit_if_released(e);
            return CONSUME;
        }
        return PASS;
    }
    if (e->suppressed_vk[vk]) {
        /* Ownership survives visibility and modifier changes; only an unchanged repeatable
           chord may invoke its action again */
        if (e->repeat_present[vk] && e->repeat_mask[vk] == modifiers && e->window_visible) {
            invoke(e, e->repeat_action[vk]);
        } else if (e->input_vk[vk]) {
            emit_key(e, vk, scan, flags, 1, 1);
        }
        return CONSUME;
    }
    set_flag(e->held_vk, &e->held_vk_count, vk, 1);
    if (!repeated) {
        if (e->key_class[vk] & CLASS_LOCK) {
            /* Que never has native focus, so it tracks every lock toggle itself */
            push_event(e, EVENT_LOCK, vk, 0, 0, 0, 0, 0, 0);
        }
        int windows_held = windows_key_held(e);
        const Hotkey *entry = NULL;
        if (!windows_held) {
            entry = find_hotkey(e, modifiers, vk, 1);
            if (entry == NULL && e->window_visible) {
                entry = find_hotkey(e, modifiers, vk, 0);
            }
        }
        if (entry != NULL) {
            set_flag(e->suppressed_vk, &e->suppressed_vk_count, vk, 1);
            if (entry->repeat) {
                e->repeat_present[vk] = 1;
                e->repeat_mask[vk] = modifiers;
                e->repeat_action[vk] = entry->action;
            }
            invoke(e, entry->action);
            return CONSUME;
        }
        if (e->input_armed) {
            int system = windows_held || (e->key_class[vk] & CLASS_WINDOWS) ||
                         is_system_chord(e, modifiers, vk);
            if (system) {
                set_armed(e, 0, 1);
            } else if (!(e->key_class[vk] & CLASS_DEVICE)) {
                set_flag(e->suppressed_vk, &e->suppressed_vk_count, vk, 1);
                e->input_vk[vk] = 1;
                emit_key(e, vk, scan, flags, 1, 0);
                return CONSUME;
            }
        }
    }
    if (e->buffered_count) {
        replay_modifiers_and_key(e, vk, scan, flags);
        return CONSUME;
    }
    return PASS;
}

static int owned_find(const QueEngine *e, int button) {
    for (int i = 0; i < e->owned_count; i++) {
        if (e->owned[i].button == button) {
            return i;
        }
    }
    return -1;
}

static void emit_pointer(QueEngine *e, int message, int x, int y, uint32_t data, int64_t hwnd) {
    push_event(e, EVENT_POINTER, message, x, y, data, held_mask(e), hwnd, 0);
}

static int process_pointer(QueEngine *e, int code, WPARAM message, int x, int y, uint32_t data,
                           int flags, uint32_t tick) {
    if (code < 0) {
        return PASS;
    }
    if (flags & LLMHF_INJECTED_FLAG) {
        return PASS;
    }
    int64_t target = 0;
    if (e->routing_enabled) {
        for (int i = 0; i < e->region_count; i++) {
            const QueRegion *r = &e->regions[i];
            if (r->left <= x && x < r->right && r->top <= y && y < r->bottom) {
                target = r->hwnd;
                break;
            }
        }
    }
    int button = 0;
    int down = 0;
    switch (message) {
    case WM_LBUTTONDOWN: button = 1; down = 1; break;
    case WM_LBUTTONUP: button = 1; break;
    case WM_RBUTTONDOWN: button = 2; down = 1; break;
    case WM_RBUTTONUP: button = 2; break;
    case WM_MBUTTONDOWN: button = 4; down = 1; break;
    case WM_MBUTTONUP: button = 4; break;
    case WM_XBUTTONDOWN: button = 8; down = 1; break;
    case WM_XBUTTONUP: button = 8; break;
    default: break;
    }
    if (button == 8) {
        button = (data >> 16) == 1 ? 8 : 16;
    }
    int wheel = message == WM_MOUSEWHEEL || message == WM_MOUSEHWHEEL;
    if (e->observe) {
        if (button) {
            push_event(e, EVENT_OBSERVE_BUTTON, button, 0, 0, (uint32_t)down, 0, 0, tick);
        } else if (wheel) {
            push_event(e, EVENT_OBSERVE_WHEEL, (int32_t)message, 0, 0, 0, 0, 0, tick);
        }
    }
    if (button) {
        if (down) {
            e->press_seq++;
            if (target && !(e->delivered_buttons & (uint32_t)button)) {
                int index = owned_find(e, button);
                if (index >= 0) {
                    e->owned[index].hwnd = target;
                } else {
                    e->owned[e->owned_count].button = button;
                    e->owned[e->owned_count].hwnd = target;
                    e->owned_count++;
                }
                e->input_armed = 1;
            } else {
                e->delivered_buttons |= (uint32_t)button;
                e->input_armed = 0;
            }
        } else {
            e->delivered_buttons &= ~(uint32_t)button;
        }
        int index = owned_find(e, button);
        int64_t owned = index >= 0 ? e->owned[index].hwnd : 0;
        emit_pointer(e, (int)message, x, y, data, owned ? owned : target);
        if (owned) {
            if (!down) {
                for (int i = index; i + 1 < e->owned_count; i++) {
                    e->owned[i] = e->owned[i + 1];
                }
                e->owned_count--;
                finish_quit_if_released(e);
            }
            return CONSUME;
        }
    } else if (wheel) {
        if (target) {
            emit_pointer(e, (int)message, x, y, data, target);
            return CONSUME;
        }
    } else if (message == WM_MOUSEMOVE) {
        int64_t drag_target = e->owned_count ? e->owned[0].hwnd : target;
        if (drag_target || e->hover_target) {
            emit_pointer(e, (int)message, x, y, data, drag_target);
        }
        e->hover_target = target;
    }
    return PASS;
}

/* ---- Live hooks ---------------------------------------------------------------------- */

static void publish_state(QueEngine *e) {
    InterlockedExchange(&e->drained, is_drained(e));
    if (e->event_head != ReadAcquire64(&e->event_tail) && e->notify_window &&
        InterlockedExchange(&e->notify_pending, 1) == 0) {
        PostMessageW(e->notify_window, e->notify_message, 0, 0);
    }
}

static void note_lateness(QueEngine *e, DWORD event_time) {
    LONG late = (LONG)(GetTickCount() - event_time);
    if (late > QUE_LATE_MS) {
        InterlockedIncrement(&e->late_events);
    }
    if (late > e->max_late_ms) {
        InterlockedExchange(&e->max_late_ms, late);
    }
}

static void send_input(const QueReplay *replays, int count) {
    INPUT inputs[QUE_MAX_REPLAYS];
    for (int i = 0; i < count; i++) {
        memset(&inputs[i], 0, sizeof(INPUT));
        inputs[i].type = INPUT_KEYBOARD;
        inputs[i].ki.wVk = (WORD)replays[i].vk;
        inputs[i].ki.wScan = (WORD)(replays[i].scan & 0xFF);
        inputs[i].ki.dwFlags = (DWORD)replays[i].flags;
    }
    SendInput((UINT)count, inputs, sizeof(INPUT));
}

/* Send this decision's replays. SendInput re-enters the hook callback for each injected event
   before it returns, so the list is taken and cleared first; otherwise every nested callback
   would send the same chord again and the chord would repeat without end. */
static void deliver_replays(QueEngine *e) {
    QueReplay pending[QUE_MAX_REPLAYS];
    int count = e->replay_count;
    memcpy(pending, e->replays, sizeof(QueReplay) * (size_t)count);
    e->replay_count = 0;
    if (count) {
        e->send(pending, count);
    }
}

/* Decide one keyboard event; the live callback and the tests share this path. */
static int handle_key(QueEngine *e, int code, WPARAM message, int vk, int scan, int flags) {
    apply_commands(e);
    int result = process_key(e, code, message, vk, scan, flags, GetTickCount());
    deliver_replays(e);
    publish_state(e);
    return result;
}

/* Decide one mouse event; the live callback and the tests share this path. */
static int handle_pointer(QueEngine *e, int code, WPARAM message, int x, int y, uint32_t data,
                          int flags) {
    apply_commands(e);
    int result = process_pointer(e, code, message, x, y, data, flags, GetTickCount());
    publish_state(e);
    return result;
}

static LRESULT CALLBACK keyboard_proc(int code, WPARAM message, LPARAM address) {
    QueEngine *e = active_engine;
    if (e == NULL || code < 0) {
        return CallNextHookEx(NULL, code, message, address);
    }
    const KBDLLHOOKSTRUCT *kb = (const KBDLLHOOKSTRUCT *)address;
    note_lateness(e, kb->time);
    int result =
        handle_key(e, code, message, (int)kb->vkCode, (int)kb->scanCode, (int)kb->flags);
    return result == CONSUME ? 1 : CallNextHookEx(NULL, code, message, address);
}

static LRESULT CALLBACK mouse_proc(int code, WPARAM message, LPARAM address) {
    QueEngine *e = active_engine;
    if (e == NULL || code < 0) {
        return CallNextHookEx(NULL, code, message, address);
    }
    const MSLLHOOKSTRUCT *data = (const MSLLHOOKSTRUCT *)address;
    if (message != WM_MOUSEMOVE) {
        note_lateness(e, data->time);
    }
    int result =
        handle_pointer(e, code, message, data->pt.x, data->pt.y, data->mouseData, (int)data->flags);
    return result == CONSUME ? 1 : CallNextHookEx(NULL, code, message, address);
}

static void uninstall_hooks(QueEngine *e) {
    if (e->keyboard_hook) {
        UnhookWindowsHookEx(e->keyboard_hook);
        e->keyboard_hook = NULL;
    }
    if (e->mouse_hook) {
        UnhookWindowsHookEx(e->mouse_hook);
        e->mouse_hook = NULL;
    }
}

static int install_hooks(QueEngine *e) {
    HMODULE module = NULL;
    GetModuleHandleExW(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS |
                           GET_MODULE_HANDLE_EX_FLAG_UNCHANGED_REFCOUNT,
                       (LPCWSTR)(void *)keyboard_proc, &module);
    int status = 0;
    e->keyboard_hook = SetWindowsHookExW(WH_KEYBOARD_LL, keyboard_proc, module, 0);
    if (e->keyboard_hook) {
        status |= STATUS_KEYBOARD;
    } else {
        InterlockedExchange(&e->keyboard_error, (LONG)GetLastError());
        return status;
    }
    e->mouse_hook = SetWindowsHookExW(WH_MOUSE_LL, mouse_proc, module, 0);
    if (e->mouse_hook) {
        status |= STATUS_MOUSE;
    } else {
        InterlockedExchange(&e->mouse_error, (LONG)GetLastError());
        uninstall_hooks(e);
        status = 0;
    }
    return status;
}

/* Keep everything the callbacks touch resident so a key after idle never waits for disk. */
static int lock_memory(QueEngine *e) {
    HANDLE process = GetCurrentProcess();
    SIZE_T minimum = 0;
    SIZE_T maximum = 0;
    if (GetProcessWorkingSetSize(process, &minimum, &maximum)) {
        SetProcessWorkingSetSize(process, minimum + (8u << 20), maximum + (8u << 20));
    }
    int ok = VirtualLock(e, sizeof(QueEngine)) != 0;
    HMODULE module = NULL;
    if (GetModuleHandleExW(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS |
                               GET_MODULE_HANDLE_EX_FLAG_UNCHANGED_REFCOUNT,
                           (LPCWSTR)(void *)keyboard_proc, &module)) {
        const IMAGE_DOS_HEADER *dos = (const IMAGE_DOS_HEADER *)module;
        const IMAGE_NT_HEADERS *nt =
            (const IMAGE_NT_HEADERS *)((const BYTE *)module + dos->e_lfanew);
        ok = VirtualLock(module, nt->OptionalHeader.SizeOfImage) && ok;
    } else {
        ok = 0;
    }
    /* Commit and lock the stack the callbacks run on */
    volatile BYTE stack[64 * 1024];
    for (size_t i = sizeof(stack); i >= 4096; i -= 4096) {
        stack[i - 1] = 0; /* Top down, the direction the stack grows */
    }
    ok = VirtualLock((void *)stack, sizeof(stack)) && ok;
    return ok;
}

static DWORD WINAPI hook_thread(LPVOID parameter) {
    QueEngine *e = (QueEngine *)parameter;
    LONG status = 0;
    if (SetThreadPriority(GetCurrentThread(), THREAD_PRIORITY_TIME_CRITICAL)) {
        status |= STATUS_PRIORITY;
    }
    if (lock_memory(e)) {
        status |= STATUS_MEMORY_LOCKED;
    }
    MSG message;
    PeekMessageW(&message, NULL, WM_USER, WM_USER, PM_NOREMOVE); /* Create the message queue */
    int installed = install_hooks(e);
    InterlockedExchange(&e->status, status | installed);
    SetEvent(e->ready);
    if (!installed) {
        return 1;
    }
    while (GetMessageW(&message, NULL, 0, 0) > 0) {
        if (message.message == WM_QUE_WAKE) {
            apply_commands(e);
            publish_state(e);
        } else if (message.message == WM_QUE_REINSTALL) {
            uninstall_hooks(e);
            installed = install_hooks(e);
            InterlockedExchange(&e->status, (e->status & ~(STATUS_KEYBOARD | STATUS_MOUSE)) |
                                                installed);
            SetEvent(e->ready);
        } else if (message.message == WM_QUE_STOP) {
            break;
        } else {
            TranslateMessage(&message);
            DispatchMessageW(&message);
        }
    }
    uninstall_hooks(e);
    return 0;
}

/* ---- Exports ------------------------------------------------------------------------- */

QUE_API QueEngine *que_create(void) {
    QueEngine *e = (QueEngine *)VirtualAlloc(NULL, sizeof(QueEngine), MEM_COMMIT | MEM_RESERVE,
                                             PAGE_READWRITE);
    if (e == NULL) {
        return NULL;
    }
    memset(e->modifier_index, -1, sizeof(e->modifier_index));
    e->send = send_input;
    e->routing_enabled = 1;
    e->drained = 1;
    return e;
}

QUE_API void que_destroy(QueEngine *e) {
    if (e == NULL) {
        return;
    }
    if (active_engine == e) {
        active_engine = NULL;
    }
    if (e->ready) {
        CloseHandle(e->ready);
    }
    if (e->thread) {
        CloseHandle(e->thread);
    }
    VirtualUnlock(e, sizeof(QueEngine));
    VirtualFree(e, 0, MEM_RELEASE);
}

QUE_API int que_add_hotkey(QueEngine *e, int mask, int vk, int action, int repeat, int always) {
    if (e->hotkey_count >= QUE_MAX_HOTKEYS) {
        return 0;
    }
    Hotkey *h = &e->hotkeys[e->hotkey_count++];
    h->mask = mask;
    h->vk = vk & 0xFF;
    h->action = action;
    h->repeat = repeat != 0;
    h->always = always != 0;
    return 1;
}

/* Declare a modifier key; call in ascending VK order so event "held" bits sort like VKs. */
QUE_API int que_add_modifier(QueEngine *e, int vk, int bit) {
    if (e->modifier_count >= 32) {
        return 0;
    }
    e->modifier_bit[vk & 0xFF] = (uint8_t)bit;
    e->modifier_index[vk & 0xFF] = (int8_t)e->modifier_count++;
    return 1;
}

QUE_API void que_set_key_class(QueEngine *e, int vk, int classes) {
    e->key_class[vk & 0xFF] = (uint8_t)classes;
}

QUE_API int que_add_system_chord(QueEngine *e, int mask, int vk) {
    if (e->chord_count >= QUE_MAX_CHORDS) {
        return 0;
    }
    e->chords[e->chord_count][0] = mask;
    e->chords[e->chord_count][1] = vk & 0xFF;
    e->chord_count++;
    return 1;
}

QUE_API void que_set_observe(QueEngine *e, int enabled) { e->observe = enabled != 0; }

QUE_API void que_set_que_key(QueEngine *e, int vk) { e->que_key = vk & 0xFF; }

#ifndef QUE_SOURCE_SHA256
#define QUE_SOURCE_SHA256 "unknown"
#endif

/* Return the SHA-256 of the que_hook.c this library was built from (set by native/build.py). */
QUE_API const char *que_source_hash(void) { return QUE_SOURCE_SHA256; }

/* Queue one state update for the hook thread; returns 0 when the ring is full. */
QUE_API int que_command(QueEngine *e, const QueCommand *command) {
    LONG64 head = e->command_head;
    if (head - ReadAcquire64(&e->command_tail) >= QUE_COMMAND_RING) {
        return 0;
    }
    memcpy(&e->commands[head & (QUE_COMMAND_RING - 1)], command, sizeof(QueCommand));
    WriteRelease64(&e->command_head, head + 1);
    if (e->thread_id) {
        PostThreadMessageW(e->thread_id, WM_QUE_WAKE, 0, 0);
    } else {
        /* Not started (tests): apply on the caller's thread */
        apply_commands(e);
        InterlockedExchange(&e->drained, is_drained(e));
    }
    return 1;
}

/* Start the hook thread and install both hooks; returns STATUS_* flags (0 on failure). */
QUE_API int que_start(QueEngine *e, HWND notify_window, UINT notify_message) {
    if (active_engine != NULL) {
        return 0;
    }
    e->notify_window = notify_window;
    e->notify_message = notify_message;
    e->ready = CreateEventW(NULL, FALSE, FALSE, NULL);
    active_engine = e;
    e->thread = CreateThread(NULL, 256 * 1024, hook_thread, e, 0, &e->thread_id);
    if (e->thread == NULL) {
        active_engine = NULL;
        e->thread_id = 0;
        return 0;
    }
    WaitForSingleObject(e->ready, INFINITE);
    if (!(e->status & STATUS_KEYBOARD)) {
        WaitForSingleObject(e->thread, INFINITE);
        CloseHandle(e->thread);
        e->thread = NULL;
        e->thread_id = 0;
        active_engine = NULL;
    }
    return (int)e->status;
}

/* Remove and reinstall both hooks (after a detected leak); returns STATUS_* flags. */
QUE_API int que_reinstall(QueEngine *e) {
    ResetEvent(e->ready); /* Never read a signal left by a reinstall that timed out */
    if (!e->thread_id || !PostThreadMessageW(e->thread_id, WM_QUE_REINSTALL, 0, 0)) {
        return 0;
    }
    if (WaitForSingleObject(e->ready, 2000) != WAIT_OBJECT_0) {
        return 0;
    }
    return (int)e->status;
}

QUE_API void que_stop(QueEngine *e) {
    if (e->thread_id) {
        PostThreadMessageW(e->thread_id, WM_QUE_STOP, 0, 0);
        WaitForSingleObject(e->thread, 5000);
        CloseHandle(e->thread);
        e->thread = NULL;
        e->thread_id = 0;
    }
    if (active_engine == e) {
        active_engine = NULL;
    }
}

/* Clear the pending wake-up before draining, so events pushed meanwhile post a new one. */
QUE_API void que_ack_notify(QueEngine *e) { InterlockedExchange(&e->notify_pending, 0); }

QUE_API int que_next_event(QueEngine *e, QueEvent *out) {
    LONG64 tail = e->event_tail;
    if (tail == ReadAcquire64(&e->event_head)) {
        return 0;
    }
    memcpy(out, &e->events[tail & (QUE_EVENT_RING - 1)], sizeof(QueEvent));
    WriteRelease64(&e->event_tail, tail + 1);
    return 1;
}

QUE_API int que_drained(QueEngine *e) { return (int)e->drained; }

QUE_API void que_stats(QueEngine *e, QueStats *out) {
    out->status = (uint32_t)e->status;
    out->keyboard_error = (uint32_t)e->keyboard_error;
    out->mouse_error = (uint32_t)e->mouse_error;
    out->late_events = (uint32_t)e->late_events;
    out->max_late_ms = (uint32_t)e->max_late_ms;
    out->dropped_events = (uint32_t)e->dropped_events;
}

/* Test entry points: decide one event on the caller's thread through the live path. */
QUE_API int que_test_key(QueEngine *e, int code, int message, int vk, int scan, int flags) {
    return handle_key(e, code, (WPARAM)message, vk, scan, flags);
}

QUE_API int que_test_pointer(QueEngine *e, int code, int message, int x, int y, uint32_t data,
                             int flags) {
    return handle_pointer(e, code, (WPARAM)message, x, y, data, flags);
}

/* Replace SendInput (tests): the sender records replays and re-enters the engine as Windows does. */
QUE_API void que_test_set_sender(QueEngine *e, QueSender send) { e->send = send; }

/* Expose internal sets for the differential test's state comparison. */
QUE_API void que_test_state(QueEngine *e, uint8_t *held_vk, uint8_t *suppressed_vk,
                            uint8_t *held_modifier, int32_t *counts) {
    memcpy(held_vk, e->held_vk, 256);
    memcpy(suppressed_vk, e->suppressed_vk, 256);
    memcpy(held_modifier, e->held_modifier, 256);
    counts[0] = e->buffered_count;
    counts[1] = e->owned_count;
    counts[2] = e->input_armed;
    counts[3] = (int32_t)e->press_seq;
}
