# MentorX

Sokratik AI tutor, model eğitim hatları ve akıllı sınav gözetim ekosistemi.

Bu depo MentorX tutor veri hatlarını, model eğitim deneylerini ve ExamGuard
uygulamalarını tek bir kod tabanında toplar.

SentialX, üretilmiş veri kümeleri, model ağırlıkları, çalışma çıktıları ve
gerçek ortam anahtarları bu klasörün kapsamında değildir.

## Yapı

```text
apps/
  examguard/
    backend/       Flask + Socket.IO sunucusu ve öğretmen paneli
    desktop/       Windows masaüstü izleme ajanı
    extension/     Chrome Manifest V3 eklentisi
pipelines/
  agentic/         Aktif üretim ve değerlendirme hattı
  dataset-preparation/
                   Temizleme, birleştirme, ChatML ve veri bölme
  legacy/          Eski tek-model üretim/değerlendirme deneyleri
training/          QLoRA eğitim scripti
notebooks/
  automata/        Otomata Teorisi model deneyleri
  python-cs1/      Python CS1 model deneyleri
tests/             Yeni ortak testlerin konumu
```

## Veri Yerleşimi

Kod ile veri birbirinden ayrı tutulmalıdır. Pipeline komutlarında girdi ve
çıktı yollarını depo dışındaki bir çalışma dizinine verin:

```text
mentorx-data/
  raw/
  generated/
  evaluated/
  processed/
  splits/
```

JSON, JSONL, model checkpoint ve ekran görüntüsü dosyalarını bu depoya
eklemeyin.

## Agentic Pipeline

```powershell
cd pipelines\agentic
python -m pip install -r requirements.txt
Copy-Item .env.example .env
python agentic_generate_pipeline.py --question-bank C:\path\question_bank.json --output-dir C:\path\generated
python agentic_evaluate_pipeline.py --output-dir C:\path\generated
```

Üretim ve değerlendirme scriptleri ortak kodu
`agentic_error_dialog_pipeline.py` üzerinden kullanır. Bu üç dosyayı aynı
klasörde tutun.

## ExamGuard

Backend:

```powershell
cd apps\examguard\backend
python -m pip install -r requirements.txt
Copy-Item .env.example .env
python server.py
```

Öğretmen paneli varsayılan olarak `http://localhost:5000` adresindedir.

Windows masaüstü ajanı:

```powershell
cd apps\examguard\desktop
python -m pip install -r requirements.txt
python desktop_agent.py
```

Chrome eklentisini yüklemek için `apps/examguard/extension` klasörünü
Chrome'un "Paketlenmemiş öğe yükle" ekranından seçin.

## Eğitim

`training/train_colab_qlora_sft.py` ve `notebooks/` altındaki çalışmalar GPU
ortamı, tercihen Colab üzerinde kullanılmak üzere hazırlanmıştır.
