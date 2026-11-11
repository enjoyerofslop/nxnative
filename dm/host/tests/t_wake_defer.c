// A thread woken or started by a higher-priority thread pinned to its core runs only once that thread gives the core up (wake_defer, hle_thread.c), through real thread SVCs.
// The test's main thread (priority 44, core 0) wakes a worker, keeps running for 20 ms (a host sleep, which does not give the core up), writes, then gives the core up in one of
// several ways; the worker records what it saw. Super Mario Odyssey's FileLoadThread pushes a request's "loaded" message and then writes its state, and the waiters must not
// overtake that write. Cases where Horizon's rule does not hold the worker (equal priority, another core, a worker or a waker that can run on another core, a host-created worker),
// and the rule turned off, must let the worker see the value from before the write
#include "../hle_thread.c"
#include <assert.h>
#include <unistd.h>

enum { K_START, K_MUTEX, K_COND, K_ADDR, K_EVENT };                       // how the worker waits, and so how main wakes it
enum { G_SLEEP, G_WAIT, G_REQUEST, G_NEVER, G_SHORT };                  // how main gives its core up after the write
enum { V_RULE, V_EQUAL, V_CORE, V_WORKER_MASK, V_HOST, V_WAKER_MASK, V_OFF };   // what holds the worker back, or why nothing should
static thr_t worker;
static _Atomic int shared, seen, ready;
static _Atomic uint64_t seen_ns;
static _Atomic uint32_t mutex_word, cond_key, addr_word;
static uint32_t main_handle, ev[2][2];   // [event][write, read]: 0 wakes the worker, 1 is the one main waits on and nobody signals
static int kind;

static void *work(void *arg) {
    thr_t *t = arg; pthread_setspecific(g_keythr, t);
    uint64_t r[32] = {0}; uint32_t h = ev[0][1];
    if (kind == K_START) { pthread_mutex_lock(&G); ready = 1; thr_await_start(t); pthread_mutex_unlock(&G); }
    else if (kind == K_MUTEX) { r[0] = main_handle; r[1] = (uint64_t)&mutex_word; r[2] = t->handle; svc_thread(0x1A, r); }
    else if (kind == K_COND) { atomic_store(&mutex_word, t->handle); r[0] = (uint64_t)&mutex_word; r[1] = (uint64_t)&cond_key; r[2] = t->handle; r[3] = (uint64_t)-1; svc_thread(0x1C, r); }
    else if (kind == K_ADDR) { r[0] = (uint64_t)&addr_word; r[1] = 2; r[2] = 0; r[3] = (uint64_t)-1; svc_thread(0x34, r); }
    else { r[1] = (uint64_t)&h; r[2] = 1; r[3] = (uint64_t)-1; svc_thread(0x18, r); }
    assert(r[0] == 0);
    atomic_store(&seen, atomic_load(&shared)); atomic_store(&seen_ns, now_ns());
    if (kind == K_MUTEX || kind == K_COND) { r[0] = (uint64_t)&mutex_word; svc_thread(0x1B, r); }   // it holds the mutex now
    thread_exit(t);
    return NULL;
}
static int waiting(void) {
    pthread_mutex_lock(&G); int w = kind == K_START ? ready : worker.wkind == (kind == K_MUTEX ? 1 : kind == K_COND ? 2 : kind == K_ADDR ? 3 : 4); pthread_mutex_unlock(&G);
    return w;
}
static void wake_worker(void) {
    uint64_t r[32] = {0};
    if (kind == K_START) r[0] = worker.handle, svc_thread(0x09, r);
    else if (kind == K_MUTEX) r[0] = (uint64_t)&mutex_word, svc_thread(0x1B, r);
    else if (kind == K_COND) r[0] = (uint64_t)&cond_key, r[1] = 1, svc_thread(0x1D, r);
    else if (kind == K_ADDR) r[0] = (uint64_t)&addr_word, r[1] = 0, r[2] = 0, r[3] = 1, svc_thread(0x35, r);
    else r[0] = ev[0][0], svc_thread(0x11, r);
    assert(r[0] == 0);
}
static void sleep_svc(int64_t ns) { uint64_t r[32] = {(uint64_t)ns}; svc_thread(0x0B, r); }
static void give_core(int how) {
    if (how == G_SLEEP || how == G_NEVER) sleep_svc(1000000);
    else if (how == G_WAIT) { uint32_t h = ev[1][1]; uint64_t r[32] = {0, (uint64_t)&h, 1, 1000000}; svc_thread(0x18, r); assert(r[0] == R_TIMEOUT); }
    else if (how == G_REQUEST) dm_thread_service_request();
    else { sleep_svc(30000); usleep(20000); atomic_store(&shared, 3); sleep_svc(1000000); }   // G_SHORT: a 30 µs sleep, then main runs on for 20 ms
}
static int defer_case(int k, int how, int variant) {
    static const char *kinds[] = {"StartThread", "mutex unlock", "condition signal", "address signal", "event signal"},
                      *hows[] = {"a sleep", "a guest wait", "a service request", "never (for 200 ms)", "a 30 µs sleep, then runs on"},
                      *variants[] = {"lower priority, same core", "equal priority", "another core", "worker can run on another core", "host-created worker", "waker can run on another core", "rule off"};
    kind = k;
    for (int e = 0; e < 2; e++) { uint64_t r[32] = {0}; svc_thread(0x45, r); assert(r[0] == 0); ev[e][0] = r[1]; ev[e][1] = r[2]; }
    memset(&worker, 0, sizeof worker);
    pthread_cond_init(&worker.cv, NULL); pthread_cond_init(&worker.pcv, NULL);
    worker.started = k != K_START; worker.prio = variant == V_EQUAL ? 44 : 46; worker.core = variant == V_CORE ? 1 : 0; worker.mask = variant == V_WORKER_MASK ? 7 : 1ull << worker.core;
    worker.host_owned = variant == V_HOST;
    thr_t *self = pthread_getspecific(g_keythr); if (!self) { printf("FAIL: the test's thread is not a guest thread\n"); exit(1); }
    uint64_t self_mask = self->mask;
    if (variant == V_WAKER_MASK) self->mask = 5;
    if (variant == V_OFF) g_wake_defer = 0;
    pthread_mutex_lock(&G); worker.handle = h_new(O_THREAD, &worker); worker.all = g_all; g_all = &worker; pthread_mutex_unlock(&G);
    atomic_store(&shared, 0); atomic_store(&seen, -1); atomic_store(&ready, 0);
    atomic_store(&mutex_word, k == K_MUTEX ? main_handle | WAITERS_BIT : 0); atomic_store(&cond_key, 0); atomic_store(&addr_word, 0);
    uint64_t caps = atomic_load(&g_defer_cap);
    pthread_t w; assert(!pthread_create(&w, NULL, work, &worker));
    while (!waiting()) usleep(100);
    atomic_store(&shared, 1);
    uint64_t t0 = now_ns();
    wake_worker();
    usleep(how == G_NEVER ? 200000 : 20000);   // main runs on: host time is not a guest wait
    atomic_store(&shared, 2);
    give_core(how);
    pthread_join(w, NULL);
    int s = atomic_load(&seen); uint64_t after = atomic_load(&seen_ns) - t0, capped = atomic_load(&g_defer_cap) - caps;
    self->mask = self_mask; g_wake_defer = 1;
    pthread_mutex_lock(&G); g_all = worker.all;
    unsigned slot = worker.handle - 0x1000; g_h[slot].type = O_FREE; g_h[slot].obj = NULL;
    pthread_mutex_unlock(&G);
    pthread_cond_destroy(&worker.cv); pthread_cond_destroy(&worker.pcv);
    for (int e = 0; e < 2; e++) for (int i = 0; i < 2; i++) { uint64_t r[32] = {ev[e][i]}; svc_thread(0x16, r); }
    // Held back: the write made after the wake comes first, and a waker that never gives its core up lets the worker go at the bound (and only then)
    int ok = variant != V_RULE ? s == 1 && !capped
           : how == G_NEVER ? s == 1 && capped == 1 && after >= WAKE_DEFER_MAX_NS - 5000000 && after < 190000000
           : s == 2 && !capped;
    printf("%s: %s wakes a worker (%s), the waker gives its core up by %s: the worker saw %d (written %s), after %.1f ms%s\n", ok ? "PASS" : "FAIL", kinds[k], variants[variant], hows[how],
           s, s == 1 ? "before the wake" : s == 2 ? "after the wake, before the waker gave its core up" : "after the waker ran on", after / 1e6, capped ? ", at the bound" : "");
    return ok;
}
int main(void) {
    alarm(20);
    setenv("DM_PREEMPT", "0", 1);
    setenv("DM_YIELD_BACKOFF", "0", 1);
    main_handle = svc_thread_main_init();
    int ok = 1;
    for (int k = K_START; k <= K_EVENT; k++) for (int how = G_SLEEP; how <= G_SHORT; how++) if (how != G_NEVER) ok &= defer_case(k, how, V_RULE);
    ok &= defer_case(K_EVENT, G_NEVER, V_RULE);
    ok &= defer_case(K_START, G_NEVER, V_RULE);
    for (int v = V_EQUAL; v <= V_OFF; v++) { ok &= defer_case(K_EVENT, G_SLEEP, v); ok &= defer_case(K_START, G_SLEEP, v); }
    return ok ? 0 : 1;
}
