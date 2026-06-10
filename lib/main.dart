import 'dart:async';
import 'dart:io';
import 'dart:typed_data';
import 'package:flutter/material.dart';
import 'package:flutter_local_notifications/flutter_local_notifications.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:timezone/data/latest.dart' as tzdata;
import 'package:timezone/timezone.dart' as tz;
import 'package:vibration/vibration.dart';
import 'timetable.dart';

// ===================== 설정 =====================
const int lookBackMinutes = 10; // 켠 시각 기준 과거 10분 내 출발편 = 탑승 열차
const int alarmBeforeArrivalMin = 3; // 도착 3분 전 알람
// ================================================

final FlutterLocalNotificationsPlugin notifPlugin =
    FlutterLocalNotificationsPlugin();

Future<void> main() async {
  WidgetsFlutterBinding.ensureInitialized();
  tzdata.initializeTimeZones();
  tz.setLocalLocation(tz.getLocation('Asia/Seoul'));

  const initSettings = InitializationSettings(
    android: AndroidInitializationSettings('@mipmap/ic_launcher'),
    iOS: DarwinInitializationSettings(
      requestAlertPermission: true,
      requestSoundPermission: true,
      requestBadgePermission: true,
    ),
  );
  await notifPlugin.initialize(initSettings);

  if (Platform.isAndroid) {
    final android = notifPlugin.resolvePlatformSpecificImplementation<
        AndroidFlutterLocalNotificationsPlugin>();
    await android?.requestNotificationsPermission();
    await android?.requestExactAlarmsPermission();
  }

  runApp(const TrainAlarmApp());
}

class Train {
  final TimeOfDay dep;
  final TimeOfDay arr;
  Train(this.dep, this.arr);

  DateTime depToday(DateTime now) =>
      DateTime(now.year, now.month, now.day, dep.hour, dep.minute);
  DateTime arrToday(DateTime now) {
    var d = DateTime(now.year, now.month, now.day, arr.hour, arr.minute);
    if (arr.hour < dep.hour) d = d.add(const Duration(days: 1));
    return d;
  }

  String get label => '${_fmt(dep)} 출발 → ${_fmt(arr)} 천안아산 도착';
  static String _fmt(TimeOfDay t) =>
      '${t.hour.toString().padLeft(2, '0')}:${t.minute.toString().padLeft(2, '0')}';
}

List<Train> parseTimetable(List<String> raw) {
  final list = <Train>[];
  for (final line in raw) {
    final parts = line.trim().split(',');
    if (parts.length != 2) continue;
    final d = parts[0].split(':'), a = parts[1].split(':');
    try {
      list.add(Train(
        TimeOfDay(hour: int.parse(d[0]), minute: int.parse(d[1])),
        TimeOfDay(hour: int.parse(a[0]), minute: int.parse(a[1])),
      ));
    } catch (_) {}
  }
  list.sort((x, y) =>
      (x.dep.hour * 60 + x.dep.minute) - (y.dep.hour * 60 + y.dep.minute));
  return list;
}

class TrainAlarmApp extends StatelessWidget {
  const TrainAlarmApp({super.key});
  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: '천안아산 알람',
      debugShowCheckedModeBanner: false,
      theme: ThemeData(
        colorScheme: ColorScheme.fromSeed(
            seedColor: const Color(0xFF5B2D8E), brightness: Brightness.dark),
        useMaterial3: true,
      ),
      home: const HomePage(),
    );
  }
}

class HomePage extends StatefulWidget {
  const HomePage({super.key});
  @override
  State<HomePage> createState() => _HomePageState();
}

class _HomePageState extends State<HomePage> {
  List<Train> trains = [];
  int? selectedIdx;
  DateTime? alarmTime;
  Timer? ticker;
  Timer? foregroundAlarm;
  String status = '';

  @override
  void initState() {
    super.initState();
    _loadAndDetect();
    ticker = Timer.periodic(const Duration(seconds: 1), (_) {
      if (mounted) setState(() {});
    });
  }

  @override
  void dispose() {
    ticker?.cancel();
    foregroundAlarm?.cancel();
    super.dispose();
  }

  Future<void> _loadAndDetect() async {
    final prefs = await SharedPreferences.getInstance();
    final saved = prefs.getStringList('timetable');
    trains = parseTimetable(saved ?? defaultTimetable);
    _autoDetect();
  }

  void _autoDetect() {
    final now = DateTime.now();
    int? recentIdx;
    int? nextIdx;
    for (var i = 0; i < trains.length; i++) {
      final dep = trains[i].depToday(now);
      final diff = now.difference(dep).inSeconds;
      if (diff >= 0 && diff <= lookBackMinutes * 60) {
        recentIdx = i;
      }
      if (dep.isAfter(now)) {
        nextIdx ??= i;
      }
    }
    final idx = recentIdx ?? nextIdx;
    if (idx == null) {
      setState(() {
        selectedIdx = null;
        status = '오늘 남은 열차가 없습니다';
      });
      return;
    }
    _selectTrain(idx,
        auto: recentIdx != null
            ? '출발 ${lookBackMinutes}분 내 열차 자동 감지'
            : '다음 출발편 자동 선택');
  }

  Future<void> _selectTrain(int idx, {String? auto}) async {
    final now = DateTime.now();
    final train = trains[idx];
    final arr = train.arrToday(now);
    final fire = arr.subtract(const Duration(minutes: alarmBeforeArrivalMin));

    await notifPlugin.cancelAll();
    foregroundAlarm?.cancel();

    if (fire.isAfter(now)) {
      await notifPlugin.zonedSchedule(
        0,
        '곧 천안아산역 도착!',
        '$alarmBeforeArrivalMin분 후 도착 예정입니다. 내릴 준비하세요.',
        tz.TZDateTime.from(fire, tz.local),
        NotificationDetails(
          android: AndroidNotificationDetails(
            'arrival_alarm',
            '도착 알람',
            channelDescription: '천안아산 도착 3분 전 알람',
            importance: Importance.max,
            priority: Priority.high,
            fullScreenIntent: true,
            enableVibration: true,
            vibrationPattern:
                Int64List.fromList([0, 800, 400, 800, 400, 800, 400, 800]),
            playSound: true,
            category: AndroidNotificationCategory.alarm,
          ),
          iOS: const DarwinNotificationDetails(
            presentAlert: true,
            presentSound: true,
            interruptionLevel: InterruptionLevel.timeSensitive,
          ),
        ),
        androidScheduleMode: AndroidScheduleMode.exactAllowWhileIdle,
        uiLocalNotificationDateInterpretation:
            UILocalNotificationDateInterpretation.absoluteTime,
      );

      foregroundAlarm = Timer(fire.difference(now), () async {
        if (await Vibration.hasVibrator() ?? false) {
          Vibration.vibrate(
              pattern: [0, 800, 400, 800, 400, 800, 400, 800, 400, 1500]);
        }
      });
    }

    setState(() {
      selectedIdx = idx;
      alarmTime = fire;
      status = auto ?? '수동 선택';
    });
  }

  String _countdown() {
    if (alarmTime == null) return '';
    final diff = alarmTime!.difference(DateTime.now());
    if (diff.isNegative) return '알람 시각 경과';
    final m = diff.inMinutes, s = diff.inSeconds % 60;
    return '알람까지 $m분 ${s.toString().padLeft(2, '0')}초';
  }

  @override
  Widget build(BuildContext context) {
    final train = selectedIdx != null ? trains[selectedIdx!] : null;
    return Scaffold(
      appBar: AppBar(
        title: const Text('동탄 → 천안아산'),
        actions: [
          IconButton(
            icon: const Icon(Icons.edit_calendar),
            tooltip: '시각표 편집',
            onPressed: () async {
              await Navigator.push(context,
                  MaterialPageRoute(builder: (_) => const TimetableEditor()));
              _loadAndDetect();
            },
          ),
        ],
      ),
      body: Padding(
        padding: const EdgeInsets.all(20),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            Card(
              color: Theme.of(context).colorScheme.primaryContainer,
              child: Padding(
                padding: const EdgeInsets.all(20),
                child: Column(
                  children: [
                    Text(status, style: const TextStyle(fontSize: 13)),
                    const SizedBox(height: 8),
                    Text(
                      train?.label ?? '열차 없음',
                      style: const TextStyle(
                          fontSize: 18, fontWeight: FontWeight.bold),
                      textAlign: TextAlign.center,
                    ),
                    const SizedBox(height: 12),
                    if (alarmTime != null) ...[
                      Text(
                        '⏰ ${alarmTime!.hour.toString().padLeft(2, '0')}:${alarmTime!.minute.toString().padLeft(2, '0')} 진동 알람 예약됨',
                        style: const TextStyle(fontSize: 16),
                      ),
                      const SizedBox(height: 6),
                      Text(_countdown(),
                          style: TextStyle(
                              fontSize: 22,
                              fontWeight: FontWeight.bold,
                              color: Theme.of(context).colorScheme.primary)),
                    ],
                  ],
                ),
              ),
            ),
            const SizedBox(height: 12),
            const Text('잘못 잡혔으면 아래에서 탑승 열차를 직접 선택:',
                style: TextStyle(fontSize: 13)),
            const SizedBox(height: 6),
            Expanded(
              child: ListView.builder(
                itemCount: trains.length,
                itemBuilder: (_, i) {
                  final t = trains[i];
                  return ListTile(
                    dense: true,
                    selected: i == selectedIdx,
                    selectedTileColor:
                        Theme.of(context).colorScheme.primaryContainer,
                    leading: Icon(
                        i == selectedIdx ? Icons.train : Icons.train_outlined),
                    title: Text(t.label),
                    onTap: () => _selectTrain(i),
                  );
                },
              ),
            ),
          ],
        ),
      ),
    );
  }
}

class TimetableEditor extends StatefulWidget {
  const TimetableEditor({super.key});
  @override
  State<TimetableEditor> createState() => _TimetableEditorState();
}

class _TimetableEditorState extends State<TimetableEditor> {
  final controller = TextEditingController();

  @override
  void initState() {
    super.initState();
    _load();
  }

  Future<void> _load() async {
    final prefs = await SharedPreferences.getInstance();
    final saved = prefs.getStringList('timetable') ?? defaultTimetable;
    controller.text = saved.join('\n');
  }

  Future<void> _save() async {
    final prefs = await SharedPreferences.getInstance();
    final lines = controller.text
        .split('\n')
        .map((e) => e.trim())
        .where((e) => e.isNotEmpty)
        .toList();
    await prefs.setStringList('timetable', lines);
    if (mounted) Navigator.pop(context);
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        title: const Text('시각표 편집'),
        actions: [
          TextButton(onPressed: _save, child: const Text('저장')),
        ],
      ),
      body: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          children: [
            const Text('한 줄에 한 열차: 출발,도착 (예: 09:10,09:26)\n'
                'SRT 앱/홈페이지 시각표 기준으로 수정하세요.'),
            const SizedBox(height: 8),
            Expanded(
              child: TextField(
                controller: controller,
                maxLines: null,
                expands: true,
                keyboardType: TextInputType.multiline,
                style: const TextStyle(fontFamily: 'monospace'),
                decoration:
                    const InputDecoration(border: OutlineInputBorder()),
              ),
            ),
          ],
        ),
      ),
    );
  }
}
