# `perf`, PMU и семплирующее профилирование: как CPU-счётчики превращаются во flame graph

Эта статья разбирает PMU-centric путь профилирования в Linux: от аппаратного события и счётчика CPU до `PERF_RECORD_SAMPLE`, ring buffer, `perf.data`, символизации, callchain и flame graph. Фокус не на справочнике команд `perf`, а на механике под капотом и границах ответственности между user-space инструментом, ядром и железом.

## От задачи профилирования к аппаратному механизму

Практическая задача профилирования начинается не с выбора флага для `perf`, а с вопроса к программе: где она тратит CPU и где концентрируются интересующие события. Иногда достаточно узнать, в каких функциях чаще всего оказывается исполнение. Иногда важнее не "время" само по себе, а конкретные аппаратные явления: сколько retired instructions выполнилось, где накапливаются cache misses, branch misses или циклы ожидания.

Есть два принципиально разных способа получать такие данные. Instrumentation-профилирование добавляет измерение в выбранные места программы: вокруг функции, участка кода, RPC-обработчика, аллокации или другой заранее известной границы. Такой подход хорошо отвечает на вопрос "сколько занял именно этот участок", но требует заранее решить, что измерять.

Sampling устроен иначе. Профилировщик не оборачивает каждый вызов и не записывает каждое действие программы. Он периодически получает срез состояния исполнения или срез по выбранному событию. В PMU-centric модели это выглядит так: аппаратное событие считается, счётчик доходит до заданного порога, процессор создаёт повод для прерывания, а ядро записывает sample с теми полями, которые запросил пользовательский инструмент.

В этой статье `perf` полезно держать в голове не как одну команду, а как user-space вход в несколько механизмов ядра. Нас интересует конкретный срез: Linux perf events поверх аппаратных счётчиков CPU. Команды вроде следующих здесь служат только короткими иллюстрациями:

```bash
perf stat -e cycles,instructions ./app
perf record -e cycles -c 100000 -g ./app
```

Первый пример ближе к counting: сколько событий произошло за время измерения. Второй включает sampling: после заданного количества событий ядро получает возможность записать sample, а `-g` просит добавить callchain, чтобы дальше можно было строить не только flat profile, но и flame graph.

Главная цепочка этой статьи такая:

```text
hardware event -> PMU counter -> kernel perf event -> sample -> IP/callchain -> symbols -> flame graph
```

В этой цепочке важно не смешивать стадии. Процессор считает события, но не строит flame graph. PMU хранит счётчик, но не знает про имена функций. Ядро программирует PMU и формирует records. User-space инструмент читает эти records, сохраняет их, символизирует адреса и превращает стеки в удобное представление.

Поэтому нижнеуровневая модель начинается с вопроса: что именно умеет считать процессор и как ядро превращает пользовательский запрос "профилируй cycles" или "профилируй instructions" в настройку аппаратного счётчика.

## PMU и PMC: что именно считает процессор

PMU, Performance Monitoring Unit, - это аппаратный блок performance monitoring внутри CPU. Его задача - наблюдать за событиями микроархитектуры и давать software доступ к счётчикам этих событий. PMC, Performance Monitoring Counter, - конкретный счётчик, значение которого увеличивается, когда выбранное событие происходит.

В один момент времени один programmable counter имеет одну конфигурацию события. Например, один счётчик можно настроить на retired instructions, другой - на branch misses, третий - на cache-related event. Если нужно одновременно измерять instructions и branch misses как независимые hardware events, это два разных потока событий: им нужны два аппаратных счётчика, fixed counter плюс programmable counter, либо multiplexing, где события получают PMU по очереди.

У программируемого счётчика есть две стороны:

- конфигурация: что считать и в каких режимах считать;
- значение: текущее число уже посчитанных событий.

На Intel/x86 эта пара дальше проявится как event-select MSR и counter MSR. Но сама идея шире конкретной архитектуры: сначала software выбирает event, затем PMU начинает инкрементировать связанный с ним counter.

Кроме programmable counters у процессоров часто есть fixed-function counters. Они считают заранее заданные базовые события и не требуют полного выбора event code так, как programmable counters. На конкретной архитектуре это могут быть, например, базовые счётчики для instructions или cycles. Их удобно воспринимать как отдельный аппаратный ресурс рядом с программируемыми slots, а не как бесконечное расширение числа обычных counters.

Три близких термина стоит развести сразу:

| Термин | Что означает |
|---|---|
| Hardware event | Событие, которое умеет наблюдать PMU: например retired instruction, branch miss или событие, связанное с кешем. |
| PMC | Физический или архитектурно видимый счётчик, который инкрементируется при наступлении выбранного события. |
| Perf event | Kernel-объект Linux perf, созданный по запросу user-space и сопоставленный подходящему PMU, software-событию или другому backend'у. |

Когда пользователь пишет `perf stat -e instructions,cycles ./app`, имена `instructions` и `cycles` выглядят переносимыми. Но внутри ядро всё равно должно сопоставить такой запрос с тем, что есть на текущем CPU: generalized hardware event, fixed counter, programmable counter, raw event code или комбинация ограничений конкретной модели процессора.

Физических счётчиков мало. Их число зависит от архитектуры, модели CPU, logical CPU, типа PMU и того, какие события можно совместить друг с другом. Поэтому запрос "посчитать десять разных событий одновременно" не означает, что в железе есть десять свободных counters. Linux может попытаться распределить события по доступным slots, использовать event groups или multiplexing, при котором разные events получают PMU не всё время, а по очереди.

Ограниченность counters влияет и на интерпретацию результата. Если событие реально стояло на счётчике всё время измерения, его raw count можно читать прямо. Если событие multiplexed, отчёт должен учитывать, сколько времени event был enabled и сколько времени он действительно running на PMU. Эта тема появится позже при разборе контекста выполнения и multiplexing, но её причина уже здесь: PMU - конечный аппаратный ресурс, а не абстрактная таблица бесконечных счётчиков.

Ещё одно следствие: "событие" в интерфейсе `perf` и "счётчик" в процессоре - не одно и то же. Event - это то, что нужно наблюдать. Counter - аппаратное место, где накапливается значение. Kernel perf event - объект ядра, который связывает пользовательский запрос, область измерения, режим counting или sampling и конкретный backend. Когда эти уровни не смешиваются, дальше проще понять и `perf stat`, и `perf record`.

## Как ядро узнаёт возможности PMU и программирует счётчики

User-space инструмент не программирует PMU напрямую. `perf` описывает запрос через системный интерфейс, а ядро проверяет, какие PMU доступны, выбирает подходящий counter и выполняет привилегированную настройку железа. На x86 эта настройка хорошо видна через Intel-модель с `CPUID`, MSR и инструкциями `RDMSR`/`WRMSR`.

Сначала ядру нужно узнать, что умеет процессор. `CPUID` - это x86-инструкция, а `CPUID.0AH` или `CPUID leaf 0x0A` - не отдельная инструкция, а режим запроса: вызвать `CPUID` с `EAX = 0x0A`. Через этот leaf Intel architectural performance monitoring сообщает параметры PMU, включая версию, число programmable counters, ширину counters и сведения о fixed-function counters и architectural events. Реальный kernel PMU driver также учитывает модель CPU и таблицы событий, потому что не вся полезная семантика PMU укладывается в один architectural leaf.

Дальше ядро работает с Model-Specific Registers, MSR. MSR - это не general-purpose registers вроде `RAX` или `RCX`. К ним обращаются специальными инструкциями. Для `RDMSR` и `WRMSR` числовой индекс MSR передаётся в `ECX`, а 64-битное значение читается или записывается через `EDX:EAX`.

Здесь легко смешать три слоя имён, поэтому полезно держать их отдельно:

| Слой | Пример | Что это |
|---|---|---|
| Архитектурное имя из документации CPU | `IA32_PERFEVTSEL0`, `IA32_PMC0`, `IA32_PERF_GLOBAL_CTRL` | Символическое имя MSR в описании архитектуры. |
| Числовой MSR index | `0x186`, `0x0c1`, `0x38f` | Число, которое инструкция `RDMSR`/`WRMSR` получает в `ECX`. |
| C-макрос в headers конкретного проекта | `MSR_ARCH_PERFMON_EVENTSEL0`, `MSR_ARCH_PERFMON_PERFCTR0`, `MSR_CORE_PERF_GLOBAL_CTRL` | Имя в C-коде Linux, которое раскрывается в числовой индекс MSR. |

Для первых Intel architectural performance counters соответствие выглядит так:

| Роль | Архитектурное имя | MSR index | Linux x86 macro |
|---|---|---|---|
| Event selector для counter 0 | `IA32_PERFEVTSEL0` | `0x186` | `MSR_ARCH_PERFMON_EVENTSEL0` |
| Counter value для counter 0 | `IA32_PMC0` | `0x0c1` | `MSR_ARCH_PERFMON_PERFCTR0` |
| Global enable/control | `IA32_PERF_GLOBAL_CTRL` | `0x38f` | `MSR_CORE_PERF_GLOBAL_CTRL` |

Архитектурное имя помогает читать документацию. Числовой индекс нужен инструкции. C-макрос нужен исходному коду, чтобы не писать magic number руками. Поэтому фраза "`IA32_PERFEVTSEL0` записывается через `WRMSR`" означает: software кладёт MSR index `0x186` в `ECX`, кладёт значение конфигурации в `EDX:EAX` и выполняет `WRMSR`. В Linux-коде это число может прийти из макроса `MSR_ARCH_PERFMON_EVENTSEL0`.

Концептуально это выглядит так:

```asm
; Записать event selector для первого programmable counter.
mov ecx, 0x186      ; MSR index: IA32_PERFEVTSEL0
mov eax, config_lo  ; младшие 32 бита конфигурации
mov edx, config_hi  ; старшие 32 бита конфигурации
wrmsr

; Инициализировать или предзагрузить значение counter.
; Для counting это может быть стартовое значение, например 0.
; Для sampling это preload, рассчитанный под overflow через sample_period.
mov ecx, 0x0c1      ; MSR index: IA32_PMC0
mov eax, value_lo
mov edx, value_hi
wrmsr
```

`IA32_PERFEVTSELx` задаёт, что и как считать. Важные поля внутри него:

- `Event Select` - базовый код события;
- `UMASK` - уточнение события, без которого многие event codes слишком общие;
- `USR` - считать события при исполнении user-space;
- `OS` - считать события при исполнении kernel-space;
- `EN` - включить конкретный counter;
- `INT` - сгенерировать interrupt при overflow.

`IA32_PMCx` хранит значение счётчика. Запись в этот MSR на этапе настройки не означает "прочитать уже насчитанное"; это инициализация состояния counter перед запуском. В counting mode ядро может стартовать счётчик с нуля или восстановленного значения, а затем позже читать накопленный count. В sampling mode ядро загружает counter так, чтобы overflow произошёл через заданный период: например, через `sample_period` событий. После overflow PMU генерирует PMI, Performance Monitoring Interrupt, а kernel handler перезагружает counter для следующего периода и собирает sample record.

На Intel есть и глобальные control MSR, например `IA32_PERF_GLOBAL_CTRL`. Одного поля `EN` в `IA32_PERFEVTSELx` недостаточно как полной ментальной модели: конкретный counter должен быть настроен, разрешён локально и разрешён на соответствующем глобальном уровне. Точные детали зависят от семейства CPU и вида counter, но роль global control проста: управлять включением набора performance counters.

Компактная схема x86-пути:

```{mermaid}
flowchart TD
    A[perf_event_open request] --> B[Kernel PMU driver]
    B --> C[Discover PMU capabilities]
    C --> D[Choose available counter]
    D --> E[Program event selector MSR]
    D --> F[Initialize or preload counter MSR]
    E --> G[Enable counter and global control]
    F --> G
    G --> H[PMU counts selected events]
    H --> I[Later: read accumulated count or handle overflow]
```

Из user-space этот путь начинается не с `WRMSR`, а с `perf_event_open()`. Пользовательский инструмент описывает event, область измерения и режим работы; kernel perf subsystem создаёт perf event; архитектурный PMU driver проверяет ограничения и программирует регистры. Это разделение важно: `WRMSR` - привилегированная инструкция, а `perf` работает через ядро.

На ARM и других архитектурах имена регистров и инструкции другие. Вместо Intel MSR будут архитектурные системные регистры и свой набор event type/control registers. Но общая модель остаётся той же: обнаружить возможности PMU, выбрать event, связать его со счётчиком, включить counting и при необходимости настроить overflow interrupt.

## От `perf_event_open()` до sample: полный путь события

На уровне user-space весь путь начинается не с PMU-регистра, а с описания запроса. Программа, например `perf record` или собственный профайлер, заполняет `struct perf_event_attr` и передает ее в `perf_event_open()`. В этой структуре задается, какое событие нужно измерять, где его измерять и какой результат ожидается: агрегированный счетчик или поток samples.

Ключевые поля здесь удобно читать как одну фразу. `type` выбирает источник события: например generalized hardware event, raw PMU event, software event или hardware cache event. `config` уточняет конкретное событие внутри выбранного типа: `PERF_COUNT_HW_INSTRUCTIONS`, `PERF_COUNT_HW_CPU_CYCLES`, raw event code и так далее. Пара `pid/cpu` задает область измерения: task на любых CPU, task только на заданном CPU или system-wide измерение на CPU при `pid == -1`.

Важно развести уровни ответственности. CLI-режим вроде `perf record -p <pid>` означает "собрать профиль процесса" как пользовательскую операцию. User-space код `perf` для этого находит уже существующие threads процесса, открывает для них нужные logical perf events и включает inheritance для новых задач, если пользователь не отключил наследование. Ядро при этом не получает один абстрактный counter "на программу целиком": `perf_event_open()` работает с task/thread и CPU-областями. Железо тоже не имеет PMC "для процесса"; физические counters стоят на CPU и получают logical perf events только тогда, когда соответствующий поток реально выполняется или когда измерение привязано к CPU.

Дальше начинается различие между counting и sampling, но это не две независимые подсистемы. Это две ветки одного perf event. В counting mode событие накапливается как число, а user-space читает результат через `read(fd, ...)`. Такой режим отвечает на вопрос: "сколько событий произошло за интервал?". В sampling mode тот же счетчик используется как периодический триггер: после заданного числа событий или с заданной частотой ядро записывает sample record. Такой режим отвечает на другой вопрос: "где в программе возникали эти события и какие стеки при этом наблюдались?".

Для sampling важны еще три поля `perf_event_attr`. `sample_period` задает фиксированный период: например записывать sample после каждого миллиона retired instructions. `sample_freq` используется в том же union-поле при включенном флаге `freq` и просит ядро подбирать период динамически, чтобы приблизиться к целевой частоте samples в секунду. `sample_type` задает состав будущей записи: `PERF_SAMPLE_IP`, `PERF_SAMPLE_TID`, `PERF_SAMPLE_TIME`, `PERF_SAMPLE_CPU`, `PERF_SAMPLE_CALLCHAIN`, `PERF_SAMPLE_READ` и другие флаги. CPU не знает про эти поля как про формат записи. Они нужны ядру, чтобы понять, какие данные собрать в момент срабатывания perf event.

Стороны API и низкоуровневой настройки полезно держать рядом, но их нельзя смешивать. На уровне user-space запрос выглядит примерно так:

```c
struct perf_event_attr attr = {
    .type = PERF_TYPE_HARDWARE,
    .config = PERF_COUNT_HW_INSTRUCTIONS,
    .sample_period = 1000000,
    .sample_type = PERF_SAMPLE_IP |
                   PERF_SAMPLE_TID |
                   PERF_SAMPLE_TIME |
                   PERF_SAMPLE_CALLCHAIN,
};

int fd = perf_event_open(&attr, pid, cpu, -1, flags);
void *buf = mmap(NULL, mmap_size, PROT_READ | PROT_WRITE,
                 MAP_SHARED, fd, 0);

ioctl(fd, PERF_EVENT_IOC_ENABLE);
```

А ниже, внутри kernel/low-level x86-части, тот же запрос концептуально превращается в настройку MSR. Это не код, который пользовательская программа может выполнить напрямую: `WRMSR` и `RDMSR` привилегированы, а конкретные индексы, биты и ограничения зависят от CPU model и kernel PMU driver.

```asm
; Настроить event selector для первого programmable counter.
mov ecx, 0x186        ; IA32_PERFEVTSEL0
mov eax, config_lo    ; event select | umask | USR | OS | INT | EN
mov edx, config_hi
wrmsr

; Предзагрузить counter под overflow через sample_period.
mov ecx, 0x0c1        ; IA32_PMC0
mov eax, preload_lo
mov edx, preload_hi
wrmsr

; Разрешить нужные counters на глобальном уровне.
mov ecx, 0x38f        ; IA32_PERF_GLOBAL_CTRL
mov eax, global_lo
mov edx, global_hi
wrmsr

; Позднее: прочитать accumulated counter value.
mov ecx, 0x0c1        ; IA32_PMC0
rdmsr

; Или использовать low-level PMC read path под контролем ядра.
mov ecx, pmc_index
rdpmc
```

После `perf_event_open()` ядро создает kernel perf event и возвращает file descriptor. Этот fd является handle'ом на объект ядра, а не просто "счетчиком в user-space". Kernel perf core проверяет права, область измерения, group constraints и формат запроса, а затем передает архитектурно-зависимую часть PMU-драйверу. Драйвер выбирает подходящий hardware counter, сопоставляет переносимое `type/config` с реальным event selector, настраивает фильтры user/kernel mode и программирует регистры PMU на том CPU, где event должен быть активен. На x86 это в конечном счете выражается в настройке MSR вроде `IA32_PERFEVTSELx`, `IA32_PMCx` и `IA32_PERF_GLOBAL_CTRL`; на других архитектурах имена регистров и инструкции другие, но роль та же: выбрать событие, включить счетчик и, если нужен sampling, включить interrupt-on-overflow.

В counting ветке дальнейшая механика минимальна. Пока event scheduled on PMU, CPU инкрементирует выбранный PMC при наступлении выбранного hardware event. Ядро учитывает включение, выключение, context switch, multiplexing и накопленное значение. Когда user-space вызывает `read(fd, ...)`, он получает агрегированный count, а при соответствующем `read_format` еще и поля вроде `time_enabled` и `time_running`. В этой ветке не нужен `PERF_RECORD_SAMPLE`: интерес представляет итоговая сумма.

Чтобы получать sampled records, user-space делает `mmap()` того же fd. Этот mapping не является файлом с путем на файловой системе. Он отображает perf event buffer: metadata page и набор data pages, которые работают как ring buffer между ядром и user-space. В metadata page находятся, среди прочего, `data_head`, `data_tail`, `data_offset` и `data_size`. Ядро пишет records в область данных и продвигает `data_head`; user-space читает records до этого head и после обработки продвигает `data_tail`. Именно через этот shared buffer `perf record` получает поток kernel records и затем сохраняет их в `perf.data`.

Для period-based PMU sampling ядро не ждет, пока счетчик просто станет равен нулю в абстрактном смысле. Оно программирует PMC так, чтобы overflow произошел после нужного числа событий: например preload'ит счетчик отрицательным значением или эквивалентным архитектурным способом. CPU дальше делает только аппаратную работу: считает retired instructions, cycles, cache misses или другое выбранное событие. Когда счетчик переполняется, PMU генерирует PMI, Performance Monitoring Interrupt.

PMI переводит управление в kernel handler. Этот обработчик определяет, какой perf event сработал, обновляет accounting, перезагружает счетчик на следующий период и собирает sample context. Если в `sample_type` был запрошен `PERF_SAMPLE_IP`, ядро берет instruction pointer из interrupt context. Если были запрошены `TID`, `TIME` или `CPU`, добавляет task id, timestamp и номер CPU. Если был запрошен `PERF_SAMPLE_CALLCHAIN`, ядро пытается собрать callchain выбранным способом или сохраняет данные, необходимые для поздней размотки. Если был запрошен `PERF_SAMPLE_READ`, в sample попадают связанные значения счетчиков.

После этого ядро формирует record с заголовком perf event record и типом `PERF_RECORD_SAMPLE`. Важная граница ответственности: CPU считает событие и доставляет прерывание, но не строит `PERF_RECORD_SAMPLE` и не пишет `perf.data`. Sample record формирует ядро. Оно сериализует поля в порядке, определяемом `sample_type`, кладет запись в mmap ring buffer и публикует новый `data_head`. User-space сторона, например `perf record`, видит продвинувшийся `data_head`, читает записи от своего `data_tail`, переносит их в файл `perf.data` вместе с метаданными сессии и затем обновляет `data_tail`. Если reader отстает от writer'а, часть данных может быть потеряна, и это тоже отражается perf records вроде lost events.

Сквозной путь выглядит так:

```{mermaid}
flowchart TD
    A["user-space: perf_event_attr"] --> B["perf_event_open() -> fd"]
    B --> C["kernel perf event"]
    C --> D["PMU driver programs counter"]
    D --> E{"mode"}

    E -->|"counting"| F["CPU increments PMC"]
    F --> G["read(fd) -> aggregate count"]

    E -->|"sampling"| H["mmap(fd) ring buffer"]
    H --> I["counter preload / sample period"]
    I --> J["CPU increments PMC"]
    J --> K["overflow -> PMI"]
    K --> L["kernel PMI handler"]
    L --> M["PERF_RECORD_SAMPLE"]
    M --> N["kernel advances data_head"]
    N --> O["perf record reads records"]
    O --> P["user-space advances data_tail"]
    P --> Q["perf.data"]
```

Эта схема объясняет, почему `perf stat` и `perf record` могут использовать похожий нижний слой, но дают разные артефакты. В обоих случаях `perf_event_open()` создает kernel perf event, а PMU считает hardware events. В counting режиме итогом является число, прочитанное из fd. В sampling режиме overflow превращается в PMI, PMI приводит ядро в обработчик, обработчик создает `PERF_RECORD_SAMPLE`, ring buffer передает records user-space, а `perf record` уже сохраняет их в `perf.data`. Поэтому sample - это не "сырое сообщение от CPU", а kernel record, построенный вокруг аппаратного факта: счетчик достиг условия срабатывания.

## Perf ring buffer: как ядро передаёт samples в user-space

В sampling-ветке `mmap()` создаёт не файл с профилем, а shared buffer между ядром и user-space reader'ом. Ядро пишет туда perf records, а `perf record` или другой reader вычитывает их и уже потом сохраняет поток данных в `perf.data`.

### Синхронизация доступа

Это producer-consumer схема. Producer - kernel perf code, который формирует records после срабатывания события. Consumer - user-space код, который читает records из отображённой области памяти. В normal non-overwrite варианте perf buffer есть metadata page со структурой `perf_event_mmap_page`; в ней находятся `data_head`, `data_tail`, `data_offset`, `data_size` и другие поля.

Важная дисциплина ownership такая:

- `data_head` публикует ядро: до какого места в data area записаны records;
- `data_tail` публикует user-space: до какого места records уже прочитаны и место можно переиспользовать;
- payload records лежат в circular data area после metadata page;
- physical wraparound считается через размер буфера, но логические `head`/`tail` монотонно растут.

```{mermaid}
flowchart LR
    K["kernel writer"] -->|"write records"| B["mmap data pages"]
    K -->|"publish data_head"| M["metadata page"]
    U["user-space reader"] -->|"read data_head"| M
    U -->|"consume records"| B
    U -->|"publish data_tail"| M
    M -->|"free space / used space"| K
```

На слабых memory models одного факта "есть два числа head/tail" недостаточно. Нужно ещё гарантировать порядок видимости. Ядро должно сначала записать record payload, а только потом опубликовать новый `data_head`. User-space должен сначала увидеть актуальный `data_head`, затем прочитать payload, и только после этого сдвинуть `data_tail`.

В документации kernel это описывается через memory barriers:

```text
kernel writer:
  read data_tail
  write record payload
  smp_wmb()
  write data_head

user-space reader:
  read data_head
  smp_rmb()
  read record payload
  smp_mb()
  write data_tail
```

`smp_rmb()`, `smp_wmb()` и `smp_mb()` - не ключевые слова языка C. Это Linux helpers для memory ordering:

- `smp_rmb()` упорядочивает чтения: последующие loads не должны стать видимыми раньше предыдущих loads;
- `smp_wmb()` упорядочивает записи: payload record должен стать видимым до публикации `data_head`;
- `smp_mb()` - полный барьер для чтений и записей; здесь он нужен перед публикацией `data_tail`, чтобы user-space не освободил область буфера до фактического чтения records.

Если проводить аналогию с C++ memory model, это похоже на acquire/release/full fences. Очень грубо user-space сторона читалась бы так:

```cpp
auto head = data_head.load(std::memory_order_relaxed);
std::atomic_thread_fence(std::memory_order_acquire);

read_records_from_ring_buffer(tail, head);

std::atomic_thread_fence(std::memory_order_seq_cst);
data_tail.store(new_tail, std::memory_order_relaxed);
```

Это не готовый perf reader: реальный код должен учитывать ABI, wraparound, record layout, `READ_ONCE`-подобные загрузки и конкретные helpers. Но аналогия полезна для интуиции: сначала reader видит опубликованный `head`, затем acquire/read barrier не даёт чтению payload уехать перед чтением `head`, а полный барьер перед публикацией `tail` не даёт освободить область буфера до фактического чтения records.

Для читателя perf buffer важнее не язык, а соблюдение ABI-протокола. User-space код может быть написан на C, C++ или другом системном языке, если он использует готовые perf/libperf helpers или повторяет те же memory-ordering гарантии. Внутренняя модель здесь такая: kernel и user-space договариваются не через mutex, а через shared metadata, ownership head/tail и барьеры памяти.

Практический вывод для разработчика профилировщика простой: если использовать `perf`, libperf или готовые библиотеки вокруг perf events, вручную реализовывать этот протокол синхронизации не нужно. В этих инструментах уже есть нужные helpers. Но понимать head/tail и barriers полезно, чтобы не воспринимать shared buffer как "просто массив records в памяти".

### Переполнение буфера

Если reader не успевает, normal non-overwrite perf buffer не перетирает ещё не прочитанные records. Когда свободного места нет, новые records теряются; kernel учитывает число потерянных events и, когда в буфере снова появляется место, может записать `PERF_RECORD_LOST`. Это важный диагностический сигнал: профиль собран, но часть samples отсутствует.

Есть и другой режим - overwritable buffer. В нём старые данные могут вытесняться новыми, а user-space читает буфер по другой дисциплине. Для `perf record` без overwrite/AUX-режимов и текущей статьи достаточно normal ring buffer: он работает как non-overwrite producer-consumer канал, где отставание reader'а превращается в lost records, а не в незаметную корректную историю.

## Контекст выполнения: per-thread, per-process, per-CPU и multiplexing

PMU - физический аппаратный блок CPU. Счётчики PMU находятся не "в процессе" и не "в потоке", а на конкретном CPU или logical CPU, где сейчас исполняется код. Но интерфейс `perf` в Linux не заставляет пользователя мыслить только физическими счётчиками. Ядро создаёт `perf event` как kernel-объект и связывает его с областью измерения: с задачей, группой задач или CPU.

### Физический счётчик и логический perf event

Из-за этого важно различать два слоя:

- hardware counter - физический ресурс PMU, который может считать событие только тогда, когда он запрограммирован на конкретном CPU;
- perf event context - логический контекст Linux perf, где хранится набор событий, их состояние, накопленные значения, период sampling и статистика времени работы.

### Область измерения: thread, process, CPU

В per-thread режиме событие следует за конкретным потоком. Когда этот поток выполняется на CPU, kernel может назначить его `perf event` на доступный hardware counter. Когда поток перестаёт выполняться, ядро снимает соответствующий PMU state с CPU и сохраняет состояние события в perf context. При следующем запуске потока событие снова получает hardware counter и продолжает измерение уже как событие этого потока.

Например, поток A профилируется по `instructions` с периодом sampling. Он выполнил часть периода, затем произошёл context switch и на том же CPU начал выполняться поток B. В per-thread профиле B не должен "доесть" оставшийся период A: ядро сохраняет perf/PMU context A, загружает context B или оставляет счётчик свободным, а при возврате к A восстанавливает его состояние. Для этой статьи context switch важен именно как точка сохранения и восстановления perf event context; устройство планировщика само по себе здесь не требуется.

Per-process профиль в пользовательском смысле является агрегированием событий по потокам процесса. На уровне `perf_event_open()` базовая привязка идёт к задаче, то есть к конкретному thread id, а инструменты вроде `perf record -p <pid>` открывают события для нужного набора уже существующих потоков и собирают общий профиль. Для потоков, созданных после начала наблюдения, используется наследование perf events, если оно включено; для режима "только новые threads того же процесса" в современных ядрах есть отдельная настройка `inherit_thread`. Если поток мигрирует между CPU, логическое событие следует за ним: физический PMU меняется, но область измерения остаётся "этот поток" или "потоки этого процесса".

Per-CPU, или system-wide, режим отвечает на другой вопрос. Здесь событие логически привязано к CPU: кто бы ни исполнялся на этом CPU, его события входят в счёт. Такой режим полезен, когда нужно увидеть, что происходило на ядре системы в целом, а не только внутри выбранного процесса. При sampling sample всё равно может содержать текущий `TID`, `CPU`, `IP` и callchain, но основание выборки другое: срабатывает счётчик CPU, а не счётчик конкретного потока.

Параметры `pid` и `cpu` в `perf_event_open()` как раз задают эту область. Упрощённо:

- `pid` задан, `cpu = -1` - измерять задачу на любых CPU, где она будет выполняться;
- `pid = -1`, `cpu` задан - измерять всё выполнение на указанном CPU;
- заданы и `pid`, и `cpu` - измерять задачу только когда она выполняется на указанном CPU.

На практике это меняет смысл результата. Per-thread/per-process профиль отвечает: "где выбранная программа тратила циклы, инструкции или промахи?". Per-CPU профиль отвечает: "какие события происходили на этом CPU, включая user-space, kernel-space и другие задачи, если фильтры не ограничили режимы исполнения".

### Multiplexing: событий больше, чем счётчиков

Вторая причина, по которой perf event не равен физическому счётчику, - ограниченное число PMU counters. Программа может попросить больше событий, чем CPU способен считать одновременно: например, `cycles`, `instructions`, несколько cache/TLB events и branch events. Тогда kernel должен либо разместить часть событий на доступных счётчиках, либо отказать, либо выполнять multiplexing.

Multiplexing означает, что события по очереди получают доступ к hardware counters. Одно событие реально считает часть времени, затем снимается с PMU, затем возвращается позже. Для counting mode Linux хранит два важных времени:

- `time_enabled` - сколько времени событие было логически включено;
- `time_running` - сколько времени событие реально было scheduled on PMU и могло считать hardware events.

Если `time_running` меньше `time_enabled`, итоговый count может быть масштабирован. Идея масштабирования проста: сырое значение счётчика относится только к времени фактической работы, поэтому perf может оценить полный count через отношение `time_enabled / time_running`. Такая оценка полезна, но она не превращает multiplexed измерение в полностью эквивалентное одновременному измерению. Чем меньше доля `time_running`, тем осторожнее нужно сравнивать значения и тем выше статистическая неопределённость.

### Группы событий и сигналы качества

Event groups добавляют ещё одно ограничение. Группа событий просит ядро измерять события как согласованный набор: либо они scheduled вместе, либо группа не получает корректного совместного измерения. Это важно для ratios и metrics, где несколько counters должны относиться к одному и тому же интервалу исполнения. Если группа не помещается в доступные counters, perf может показать, что событие или группа не считались.

В отчётах это проявляется несколькими сигналами:

- `<not supported>` - событие не может быть сопоставлено доступной PMU на этой системе или в этом режиме;
- `<not counted>` - событие было открыто, но не получило корректного времени работы на PMU;
- высокий multiplexing или заметная разница между `time_enabled` и `time_running` - значения можно читать только с поправкой на неполное время измерения.

Главная модель здесь такая: PMU физически считает на CPU, а Linux perf делает из этого набор логических измерений. В per-thread/per-process режиме логика следует за задачами и сохраняется на context switch. В per-CPU режиме логика остаётся на CPU и считает всё выбранное выполнение на нём. Multiplexing появляется, когда логических событий больше, чем физических counters, и тогда в результат вместе с count входит вопрос качества измерения.

## Какие события считать базовыми

События PMU удобно воспринимать не как один плоский список, а как несколько слоёв. Верхний слой даёт переносимые имена `perf`, ниже находятся более конкретные cache/TLB/branch комбинации, ещё ниже - raw события конкретной PMU. Отдельно стоят fixed counters как аппаратный ресурс с заранее заданной семантикой и metrics как производные показатели над несколькими events.

```{mermaid}
flowchart TD
    A["perf/PMU events"] --> B["Generalized hardware events"]
    A --> C["Hardware cache events"]
    A --> D["Raw PMU events"]
    A --> E["Fixed counters"]
    A --> F["Metrics и metric groups"]

    B --> B1["cycles, ref-cycles"]
    B --> B2["instructions"]
    B --> B3["branches, branch-misses"]
    B --> B4["cache refs/misses, stalls"]

    C --> C1["объект: L1D, L1I, LL/LLC, DTLB, ITLB, BPU"]
    C --> C2["операция: read, write, prefetch"]
    C --> C3["результат: access или miss"]

    D --> D1["vendor/model-specific config"]
    E --> E1["предзаданные аппаратные счётчики"]
    F --> F1["IPC, CPI, miss rate, Top-Down"]
```

Generalized hardware events - это базовый переносимый словарь Linux perf для аппаратных событий. К нему относятся `cycles`, `ref-cycles`, `instructions`, `branches`, `branch-misses`, `cache-references`, `cache-misses`, `bus-cycles`, `stalled-cycles-frontend` и `stalled-cycles-backend`. На уровне `perf_event_open()` такие события идут через `type = PERF_TYPE_HARDWARE`, а конкретное событие выбирается через `config`.

Эти имена полезны как общий язык между CPU, но они не делают микроархитектуры одинаковыми. `instructions` в этом слое означает retired instructions: инструкции, которые дошли до архитектурного завершения. Это не то же самое, что все инструкции или микрооперации, которые CPU мог начать спекулятивно. `cycles` ближе к core cycles, а `ref-cycles` - к reference cycles, меньше зависящим от динамического изменения частоты, если платформа предоставляет такой счётчик. `cache-references` и `cache-misses` особенно важно читать осторожно: переносимое имя скрывает CPU-specific mapping, и на разных PMU оно может означать разные уровни или разные приближения.

Hardware cache events дают более структурированный запрос. Для `PERF_TYPE_HW_CACHE` `config` кодирует тройку:

1. объект: `L1D`, `L1I`, `LL`/`LLC`, `DTLB`, `ITLB`, `BPU`, иногда `NODE`;
2. операция: `read`, `write` или `prefetch`;
3. результат: `access` или `miss`.

Так можно запросить не просто "cache misses", а, например, промахи чтения в data TLB или обращения к last-level cache. Этот слой всё ещё является generalized ABI Linux perf: ядро пытается сопоставить такую тройку реальным событиям PMU. Если CPU не умеет предоставить конкретную комбинацию, событие может оказаться недоступным. Даже когда событие доступно, его точная семантика остаётся привязанной к реализации CPU и kernel mapping.

Raw PMU events нужны, когда переносимого слоя не хватает. Они задаются через `PERF_TYPE_RAW` и числовой `config`, который соответствует документации конкретного vendor/model PMU: event select, umask и дополнительные биты конфигурации. На этом уровне появляются события вроде load latency, offcore response, uops-related events, machine clears, resource stalls и другие тонкие микроархитектурные сигналы. Raw events дают больше точности, но хуже переносимость: значение, корректное для одной модели CPU, может быть неверным или бессмысленным для другой.

Fixed counters - это аппаратные счётчики с заранее заданными событиями. На x86, например, часть базовых событий может считаться fixed-function counters, а не programmable counters. Практически это важно по двум причинам. Во-первых, fixed counter не нужно программировать так же свободно, как generic programmable counter: его смысл задан архитектурой или моделью CPU. Во-вторых, наличие fixed counters может снижать давление на ограниченный набор programmable counters, но не отменяет ограничений PMU целиком. Для текста статьи достаточно помнить: fixed counter - это физический способ считать некоторые базовые события, а не отдельный высокоуровневый класс анализа.

Metrics и metric groups находятся выше отдельных counters. IPC считается как `instructions / cycles`, CPI - как обратное отношение, miss rate - как доля misses от accesses или references. Более сложные группы, например Top-Down analysis, комбинируют несколько событий и иногда требуют специальных событий или fixed counters конкретной архитектуры. Поэтому metric - это интерпретация над событиями, а не обязательно один аппаратный event.

Эта карта помогает читать вывод `perf list`. Если событие находится в generalized hardware layer, оно удобнее для первого профиля и межмашинного сравнения, но может быть грубым. Если событие cache/TLB/branch-специфично, нужно понимать тройку "что измеряется, какая операция, какой результат". Если событие raw, нужно сверяться с документацией CPU. Если это metric, нужно смотреть, какие events стоят под формулой и не пострадали ли они от multiplexing.

## IP, символы и строки кода

`PERF_SAMPLE_IP` в sample record - это не имя функции и не строка исходного файла. Это instruction pointer, то есть runtime virtual address в том адресном пространстве, где выполнялся код: user-space процесс, shared library, JIT mapping или kernel text. Чтобы такой адрес стал понятным человеку, его нужно символизировать.

Символизация начинается с карты executable mappings. Во время записи профиля `perf` сохраняет не только samples, но и records о том, какие файлы были отображены в память процесса: `PERF_RECORD_MMAP` и, в более подробном варианте, `PERF_RECORD_MMAP2`. Эти records позволяют позднее ответить на первый вопрос: в какой mapped object попал адрес из sample.

Упрощённый путь выглядит так:

1. В sample есть runtime address, например `0x7f...`.
2. По `MMAP`/`MMAP2` records находится mapping, диапазон которого покрывает этот адрес.
3. Runtime address нормализуется относительно mapping: из него получается адрес внутри ELF/shared object или file offset с учётом смещения mapping.
4. В symbol table ищется функция, чей диапазон покрывает этот адрес.
5. Если доступны debug info, адрес можно сопоставить с исходным файлом, строкой и inline frames.

На уровне отчёта это превращает сырой адрес в форму вроде:

```text
libfoo.so`parse_message+0x34
src/parser.cc:128
```

Symbol table даёт имя функции и смещение внутри неё. Debug info добавляет более богатую, `addr2line`-подобную привязку: `file:line`, информацию о вложенных inline-вызовах, иногда более точные границы исходных конструкций. Поэтому два профиля могут иметь одинаковые samples, но разную читаемость: с одними только динамическими символами видны функции, а с debug info становится понятно, какие строки и inlined-функции стоят за тем же адресом.

Здесь важно не смешивать две разные стадии. Символизация (`symbolization`) называет адрес: какой binary object, какая функция, какая строка. Размотка стека (`unwind`) восстанавливает caller frames: кто вызвал текущую функцию, кто вызвал caller'а и так далее. Debug symbols помогают символизации и могут дать inline-информацию, но сами по себе не превращают один IP в стек вызовов. Для callchain нужен отдельный механизм размотки стека: frame pointers, DWARF CFI, LBR или другой источник callchain.

Без callchain символизация всё равно полезна. Она даёт flat profile: список функций или строк, в которых чаще всего оказывались samples. Такой отчёт отвечает на вопрос "где был instruction pointer". Он не отвечает на вопрос "по какому пути вызовов выполнение туда пришло".

У символизации есть естественные границы. Stripped binaries могут не содержать нужных symbol tables. Отдельные debug files могут отсутствовать рядом с бинарём. Inlining может сделать "строку выполнения" неочевидной, потому что одна машинная инструкция относится к телу inlined-функции внутри caller'а. Tail calls могут менять привычную картину вызовов. JIT-код требует, чтобы profiler получил карту сгенерированных участков и их имена. Shared libraries и PIE-бинарники загружаются по runtime-адресам, поэтому без mapping records сырые IP плохо интерпретируемы.

Практическая модель остаётся простой: sample приносит адрес, metadata о mappings связывает адрес с объектом, symbol/debug info превращает адрес в человеческое имя. Это отдельная стадия анализа поверх уже записанных samples.

## Почему flame graph требует callchain, а не только IP

Один sample IP фиксирует точку, где выполнение находилось в момент sample. После символизации этот IP можно показать как функцию или строку исходного кода. Если собрать много таких samples и сгруппировать их только по текущей функции, получится flat profile.

Flat profile удобен как первый срез:

```text
  31.2%  app  libcrypto.so  sha256_block_data_order
  18.7%  app  app           parse_request
   9.4%  app  libc.so       memcpy
```

Такой отчёт отвечает на вопрос "в каких функциях чаще всего находился IP". Но он теряет контекст. Если `memcpy` горячая, flat profile не показывает, откуда именно она вызывалась: из парсинга запроса, из сериализации ответа, из кеша или из логирования. Одна и та же leaf-функция может быть нормальной стоимостью в одном пути и проблемой в другом.

Flame graph строится не из отдельных IP, а из стеков. Для каждого sample нужен callchain: последовательность frames от корня выполнения к текущей точке. После символизации такой stack sample можно представить в folded format:

```text
main;serve;handle_request;parse_request;memcpy 421
main;serve;handle_request;render_response;memcpy 97
main;warmup;load_dictionary;sha256_block_data_order 53
```

Строка folded stack означает: такой путь вызовов встретился указанное число раз. Одинаковые стеки сворачиваются, счётчик справа увеличивается. Именно эта агрегация даёт flame graph его форму: одинаковые prefixes становятся общими нижними блоками, а разные продолжения расходятся выше.

Ширина блока на flame graph пропорциональна числу samples, проходящих через этот frame. Если блок `parse_request` широкий, это означает, что много sample stacks содержали `parse_request` на этом уровне. Это не обязательно один долгий вызов; это статистическая масса samples, попавших в данный frame или его descendants.

Высота flame graph показывает глубину стека. Нижние frames ближе к корню: `main`, event loop, worker entrypoint. Верхние frames ближе к текущей точке выполнения, где был зафиксирован sample IP. Поэтому высокая башня означает глубокий путь вызовов, а не большую длительность сама по себе.

X-позиция на flame graph не является временем. Соседние блоки слева и справа не означают, что один исполнялся до другого. Инструменты раскладывают aggregated stacks по горизонтали так, чтобы показать ширину и иерархию; порядок часто определяется сортировкой имён или внутренним порядком агрегации. Flame graph из sampling - это статистическая карта stack samples, а не timeline trace.

Отсюда главное требование: для flame graph нужен callchain. Один IP даёт только верхушку стека и годится для flat profile. Callchain добавляет путь вызовов, благодаря которому можно отличить "эта функция сама часто исполняется" от "эта функция часто появляется внутри конкретного сценария".

В контексте PMU sampling это особенно важно. Если sample event - `cycles`, широкие блоки показывают, где статистически концентрировалось on-CPU время. Если event - `instructions`, `cache-misses` или `branch-misses`, ширина показывает распределение выбранного аппаратного события по stack paths. В обоих случаях flame graph остаётся агрегированной статистикой samples, а не точной записью каждого вызова.

## Как perf восстанавливает callchain

Callchain не появляется из `PERF_SAMPLE_IP` сам по себе. IP даёт текущую точку выполнения, а список caller frames нужно восстановить отдельным способом. В `perf` это может происходить при записи sample, при последующем анализе `perf.data` или в смешанной схеме, где запись сохраняет достаточно состояния для поздней размотки.

В native user-space профилировании чаще всего встречаются три модели: frame pointers, DWARF unwind и LBR. Они отвечают на один вопрос - как из текущего frame получить caller state, - но используют разные источники данных.

### Frame pointers

Frame-pointer unwind опирается на соглашение о layout'е stack frame. В типичной x86-64 модели пролог функции сохраняет предыдущий `RBP` на стек, затем записывает в `RBP` адрес текущего frame. После этого `RBP` становится устойчивым якорем: относительно него находятся сохранённый frame pointer caller'а и return address.

Это соглашение появляется прямо из инструкций входа в функцию:

```asm
; caller
call foo              ; CPU pushes return address:
                      ;   rsp -= 8
                      ;   [rsp] = address after call

; callee: foo
push rbp              ; rsp -= 8; [rsp] = old rbp
mov  rbp, rsp         ; rbp becomes the frame base; [rbp] = old rbp
sub  rsp, <locals>    ; stack space for locals/spills
```

После `push rbp` вершина стека `RSP` указывает на ячейку, куда только что сохранили старый `RBP`. Инструкция `mov rbp, rsp` копирует этот адрес в `RBP`, и с этого момента `RBP` становится базой текущего frame. Важно разделять адрес и значение по адресу: в регистре `RBP` лежит адрес начала frame, а по этому адресу, в `[RBP]`, лежит адрес предыдущего frame.

Стек на x86-64 растёт к меньшим адресам. Поэтому если рисовать память сверху вниз по убыванию адресов, return address визуально находится выше сохранённого `RBP`, хотя offsets остаются такими же: `saved previous RBP` лежит по `[RBP + 0]`, а return address - по `[RBP + 8]`.

```text
higher addresses

[RBP + 16]  arguments / caller area
[RBP +  8]  return address into caller
[RBP +  0]  saved previous RBP        <- current RBP points here
[RBP -  8]  local variables / spills
[RBP - 16]  local variables / spills

lower addresses
```

В момент sample у profiler'а есть register snapshot. Для frame-pointer unwind в нём важны две нити:

- sampled `RIP` - точка выполнения в текущем, самом верхнем frame;
- sampled `RBP` - опорный адрес текущего frame в user stack.

Дальше шаг размотки механический. Если `fp = sampled RBP`, то unwinder читает `*(fp + 8)` как return address caller'а, а `*(fp + 0)` как previous frame pointer. Return address даёт следующую точку в callchain: это не entry point функции caller'а, а адрес продолжения в caller'е после `call`, по которому можно понять не только функцию caller'а, но и место вызова внутри неё. Previous frame pointer даёт адрес следующего frame, к которому применяется тот же layout.

В виде псевдокода:

```text
ip = sampled_rip
fp = sampled_rbp

emit_frame(ip)

while valid_frame_pointer(fp):
    caller_ip = read_u64(fp + 8)
    next_fp   = read_u64(fp + 0)

    emit_frame(caller_ip)
    fp = next_fp
```

Именно `next_fp` превращает stack frames в связный список. Return addresses идут параллельной нитью: без них profiler видел бы цепочку frame pointers, но не знал бы, какие IP положить в caller frames.

```{mermaid}
flowchart TD
    S["sample: RIP + RBP"] --> Top["верхний кадр: sampled RIP"]
    S --> F0["текущий frame по RBP"]
    F0 --> Prev0["RBP + 0: saved previous RBP"]
    F0 --> RA0["RBP + 8: return address caller"]
    Prev0 --> F1["frame caller'а"]
    RA0 --> Caller["caller IP / return site"]
    F1 --> Repeat["повторить тот же layout"]
```

Преимущество этой модели - простота. Для каждого frame достаточно нескольких чтений памяти из стека и проверки, что цепочка выглядит согласованно: frame pointer попадает в user stack, выровнен, в описанной модели указывает на более высокий адрес frame caller'а, а return address похож на исполняемый адрес. Поэтому frame-pointer unwind быстрый и удобный для sampling: profiler не должен интерпретировать сложные таблицы правил, чтобы получить следующий caller.

Практический вывод для native-кода: сборка с сохранением frame pointers, например через `-fno-omit-frame-pointer`, часто делает профилирование через `perf` проще и стабильнее. Это не про имена функций и строки исходников; это про наличие простой runtime-структуры, по которой можно пройти от текущего frame к caller frames.

### DWARF unwind

DWARF unwind решает ту же задачу без обязательной цепочки frame pointers. Вместо фиксированного layout'а он использует Call Frame Information, CFI: набор правил, которые описывают, как восстановить состояние caller'а для конкретного диапазона инструкций.

Ключевая единица здесь - FDE, Frame Description Entry. FDE покрывает диапазон адресов функции или её части и содержит unwind-информацию. По текущему IP unwinder выбирает подходящий FDE и конкретную unwind row внутри него: разные точки функции могут иметь разные правила, потому что пролог, тело и эпилог по-разному меняют stack pointer и сохранённые регистры.

Центральное понятие CFI - CFA, Canonical Frame Address. CFA задаёт опорную точку для caller frame как выражение от текущего `RSP`, `RBP` или другого регистра. Остальные правила говорят, где лежат return address и сохранённые регистры относительно CFA либо как их вычислить. В результате unwinder строит caller state: caller `RIP`, caller `RSP` и значения регистров, нужные для следующего шага.

Путь размотки в DWARF-модели такой:

1. взять текущий IP и register state;
2. найти FDE/unwind row, действующую для этого IP;
3. вычислить CFA;
4. восстановить caller `RIP`, `RSP` и нужные регистры;
5. повторить процесс уже для caller frame.

В режиме вроде `perf record --call-graph dwarf` важна цена данных. На момент sample kernel не может просто сохранить один `RBP` и пройти по простой цепочке. Ему нужно записать register state и кусок user stack, чтобы позднее, при анализе `perf.data`, user-space unwinder смог применить DWARF CFI. Поэтому DWARF callchains дороже по объёму данных и обработке, чем frame pointers.

DWARF unwind полезен тем, что может работать для кода без frame pointers, если доступны корректные unwind tables. Но это всё ещё отдельный механизм от символизации. Symbol/debug info помогает назвать адреса и показать строки; CFI/FDE/CFA описывают, как восстановить caller state по IP.

### LBR

LBR, Last Branch Record, - это аппаратная история последних taken branches на поддерживаемых Intel CPU. `perf` может использовать её как источник для user-space callchain: вместо чтения обычного стека или интерпретации DWARF CFI анализируется недавняя история переходов.

Это CPU-specific техника с ограниченной глубиной и собственными режимами работы. Её полезно знать как третий вариант восстановления callchain, но она не является универсальной заменой frame pointers или DWARF unwind.

## Точность attribution: skid, `precise_ip` и precise events

В PMU-based sampling sample появляется не в тот момент, когда user-space инструмент решил "посмотреть на программу", а после hardware event. Для команды вроде `perf record -e instructions -c 1000000 ./app` счётчик доходит до заданного периода, PMU сигнализирует overflow, процессор входит в обработку PMI, а ядро уже там фиксирует состояние и формирует `PERF_RECORD_SAMPLE`.

### Skid и `precise_ip`

На этом пути есть важная неточность: recorded IP не обязан указывать ровно на инструкцию, которая вызвала событие. Между событием в pipeline процессора и моментом, когда состояние стало доступно для записи sample, может пройти несколько инструкций. Этот зазор называют skid. В результате sample, собранный "по cache miss", "по branch miss" или "по retired instructions", может быть атрибутирован соседней инструкции, следующей инструкции или точке, до которой процессор успел дойти к моменту прерывания.

Skid особенно важен для событий, которые разработчик хочет привязать к конкретной инструкции. Если профиль нужен на уровне функций, небольшой сдвиг часто не меняет главный вывод. Если профиль используется для анализа load latency, промахов кеша, ветвлений или горячей инструкции в tight loop, разница между "событие произошло здесь" и "sample записан рядом" становится существенной.

`precise_ip` - это запрос к kernel perf subsystem ограничить skid для события, если такая точность поддержана PMU, моделью CPU и ядром. В `perf_event_attr` поле `precise_ip` кодирует ограничение:

- `0` - `SAMPLE_IP` может иметь произвольный skid;
- `1` - требуется постоянный skid;
- `2` - запрошен нулевой skid;
- `3` - нулевой skid является обязательным условием.

На уровне команд `perf` это часто проявляется как суффиксы precise events, например `:p`, `:pp`, `:ppp`, но смысл остаётся тем же: пользователь просит не просто считать event, а записывать sample с более строгой привязкой IP к инструкции события.

Когда sample действительно получен через precise-механизм и платформа может подтвердить точность, ядро помечает record флагом `PERF_RECORD_MISC_EXACT_IP`. Этот флаг относится к `PERF_RECORD_SAMPLE` и означает, что содержимое `PERF_SAMPLE_IP` указывает на фактическую инструкцию, вызвавшую event, в пределах гарантий данной платформы. Важно, что это не обещание "идеального профиля вообще"; это обещание про верхний IP относительно hardware event.

Аппаратные механизмы для снижения skid зависят от архитектуры:

- PEBS, Precise Event-Based Sampling, на Intel CPU записывает более точное состояние для поддерживаемых событий;
- IBS, Instruction-Based Sampling, на AMD CPU даёт собственный механизм семплирования и attribution;
- SPE, Statistical Profiling Extension, на Arm CPU решает похожую задачу через архитектурный profiling-механизм Arm.

Эти названия не взаимозаменяемы и не описывают один универсальный формат. Общая идея одна: вместо обычного interrupt-after-overflow пути процессор предоставляет более пригодную для attribution запись о событии или точке выполнения. Конкретные события, поля, ограничения и формат данных остаются CPU-specific.

### Цена точности

За снижение skid приходится платить несколькими вещами.

Во-первых, precise mode поддерживают не все events. Базовое событие `instructions` на одной машине может иметь пригодный precise-вариант, а нужное cache/TLB/offcore событие на другой машине может не поддерживать такую привязку. Даже когда имя события одинаково выглядит в `perf list`, фактическая поддержка precise mode определяется PMU конкретного CPU и кодом kernel PMU driver.

Во-вторых, precise events могут конкурировать за специальные аппаратные ресурсы и накладывать ограничения на группу событий. Профиль, который работает в обычном counting или sampling mode, не обязан запускаться с тем же набором events в precise mode. Поэтому precise sampling часто становится отдельным проходом: сначала найти горячую область грубым профилем, затем сузить вопрос и включить более дорогую точность.

В-третьих, точный sample может быть тяжелее обычного. Платформа может записывать больше данных на событие, использовать отдельный буфер или требовать дополнительной обработки в ядре. Это увеличивает объём `perf.data`, повышает нагрузку на ring buffers и снижает практический потолок sampling rate и детализации. Если период слишком мал, инструмент начинает терять records или сам профиль становится заметной частью нагрузки.

Итоговая цена выглядит так: меньше доступных событий, сильнее зависимость от CPU model и kernel PMU driver, больше объём данных и overhead обработки, ниже практический потолок частоты семплирования. `precise_ip` стоит включать тогда, когда вопрос действительно про attribution к инструкции, а не просто про функцию или путь вызовов.

### Две независимые оси качества

Практическая модель такая: `precise_ip` улучшает attribution события к инструкции, но не исправляет всё остальное. Он не делает символизацию богаче, не добавляет debug info, не чинит stripped binaries и не гарантирует правильный callchain. Callchain зависит от frame pointers, DWARF CFI, LBR, сохранённых регистров, stack dump и потерь данных. Можно получить precise верхний IP и плохой stack trace; можно получить хорошо размотанный стек, но верхний IP будет иметь skid относительно hardware event.

Поэтому качество профиля полезно раскладывать на две независимые оси:

- точность event attribution: насколько `PERF_SAMPLE_IP` близок к инструкции, вызвавшей hardware event;
- качество callchain unwind: насколько корректно восстановлены caller frames выше sampled IP.

Для flame graph обе оси важны, но отвечают на разные вопросы. Первая определяет, куда будет отнесён сам sample. Вторая определяет, по какому пути вызовов этот sample попадёт в folded stack.

## Сквозной сценарий целиком

Теперь можно собрать весь путь в один пример:

```bash
perf record -e instructions -c 1000000 -g ./app
```

Эта команда просит не просто запустить `./app`, а записывать sample примерно после каждого миллиона retired instructions и вместе с верхним IP сохранять callchain. Дальше происходит цепочка, в которой user-space, ядро, PMU и последующий анализ выполняют разные роли.

`perf record` подготавливает `perf_event_attr`: выбирает событие `instructions`, задаёт период через `sample_period = 1000000`, включает sampling mode и добавляет в `sample_type` поля, нужные для IP, служебных метаданных и callchain. Затем он вызывает `perf_event_open()`. Этот вызов создаёт kernel perf event и возвращает file descriptor, через который user-space будет управлять измерением и читать поток записей.

До запуска профилируемого кода `perf record` делает `mmap()` этого fd и получает ring buffer. Это не файл на файловой системе, а shared mapping perf event buffer: ядро будет писать туда records и двигать `data_head`, а user-space будет читать records и двигать `data_tail`.

Когда `./app` начинает исполняться, kernel PMU driver выбирает подходящий hardware counter, программирует PMU под retired instructions, загружает counter так, чтобы overflow случился через миллион событий, и включает interrupt-on-overflow. На x86 за этим концептуально стоят event selector, counter register и global enable; user-space этих MSR напрямую не трогает.

CPU исполняет `./app`, PMU считает retired instructions. После очередного миллиона событий counter overflow приводит к PMI. Обработчик в ядре фиксирует sampled IP и собирает запрошенные поля. В зависимости от режима call graph он либо записывает уже восстановленный callchain, например при frame pointers или LBR, либо сохраняет данные, нужные для последующей размотки, как в DWARF-режиме с регистрами и user stack. Затем ядро формирует `PERF_RECORD_SAMPLE`, записывает его в ring buffer и перезагружает counter для следующего периода.

`perf record` параллельно вычитывает records из ring buffer. В поток попадают не только samples, но и метаданные, без которых последующий анализ не сможет назвать адреса: `PERF_RECORD_MMAP`/`MMAP2`, `COMM`, `FORK`, `EXIT` и другие служебные записи. Итогом записи становится `perf.data`: сериализованный набор samples и метаданных, достаточный для offline-анализа.

На этапе анализа `perf script` читает `perf.data` и печатает samples с callchains. Пока это ещё не flame graph: это поток стеков и адресов, часть которых нужно сопоставить с символами. Symbolization использует mmap metadata, ELF-файлы, symbol tables и debug info, чтобы превратить runtime virtual addresses в функции, offsets и, где возможно, строки исходного кода.

После символизации каждый sample можно представить как стек:

```text
main;run;handle_request;parse_json;utf8_validate 1
```

Stack collapsing группирует одинаковые стеки и суммирует счётчики. Получается folded format:

```text
main;run;handle_request;parse_json;utf8_validate 137
main;run;handle_request;render_response;write_json 42
```

Flame graph строится уже из folded stacks. Ширина каждого блока пропорциональна числу samples, прошедших через этот frame, высота показывает глубину стека, а позиция по горизонтали не является временем. Для выбранного события `instructions` такая картинка показывает, где статистически концентрировались retired instructions и через какие пути вызовов они проходили.

```{mermaid}
flowchart LR
    A["perf record<br/>-e instructions -c 1000000 -g ./app"] --> B["perf_event_attr<br/>perf_event_open()"]
    B --> C["kernel perf event<br/>PMU setup"]
    C --> D["counter overflow<br/>PMI"]
    D --> E["PERF_RECORD_SAMPLE<br/>IP + данные callchain"]
    E --> F["mmap ring buffer"]
    F --> G["perf.data"]
    G --> H["perf script<br/>symbolization"]
    H --> I["folded stacks"]
    I --> J["flame graph"]
```

Главная ментальная модель: `perf`-профиль не появляется как готовая картинка внутри CPU. CPU считает events и сообщает об overflow. Ядро превращает это в structured records с IP, callchain и метаданными. User-space сохраняет поток records в `perf.data`, а потом отдельный анализ превращает адреса и стеки в folded stacks и flame graph. На каждом этапе есть собственные ограничения: доступность PMU events, sampling period, skid, потери ring buffer, качество unwind и полнота debug/symbol information.
