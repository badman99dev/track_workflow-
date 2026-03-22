# 🔍 Track Workflow Monitor

Ye repo automatically **error404unknownuser99-ux/Claude** repo ke workflow runs ko monitor karta hai.

## 📁 Output Structure

```
output/
└── logs/
    ├── all_logs.txt        ← Har run ka poora log
    ├── errors.txt          ← Sirf errors
    └── last20lines.txt     ← Latest 20 lines
```

## ⚡ How it works
- Har 10 min mein automatically trigger hota hai
- Claude repo ke latest workflow run ka log fetch karta hai
- Logs ko parse karke alag-alag files mein save karta hai
- Sab kuch is repo mein commit ho jaata hai
