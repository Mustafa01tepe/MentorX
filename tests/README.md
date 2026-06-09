# Tests

Depo kökünden standart kütüphane testlerini çalıştırın:

```powershell
python -m unittest discover -s tests -p "test_*.py"
node tests\test_examguard_content.js
```

ExamGuard backend entegrasyon testleri için önce backend bağımlılıklarını
kurun:

```powershell
python -m pip install -r apps\examguard\backend\requirements.txt
```
