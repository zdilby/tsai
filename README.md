## **Project Overview**

**TSAI** is an chat system base on FastAPI and Gemini. It provides three core functionalities:

1. General chat queries handled by the Gemini API.
2. Uses RAG (Retrieval-Augmented Generation)
3. Supports uploading materials to corresponding sessions.

In addition, the application features user registration, login, profile management, and chat history storage.

## **Features**

- **User Authentication**: Users can sign up, log in, and manage their profiles.
- **Chatbot Interaction**: Users can ask general questions and receive responses powered by the Gemini API.
- **Google Support**: Get the latest infomation based on custom queries via Google API.
- **Upload System**: Users can upload personal materials to enhance session capabilities.
- **Chat History**: Chat conversations are stored in the database, and users can view past interactions.

## **Project Structure**

```bash
tsai/
│
├── backend/
│   ├── __init__.py
│   ├── db.py
│   ├── rag.py
├── logs/
├── midware/
│   ├── __init__.py
│   ├── tools.py
│   ├── upload.py
├── static/
│   ├── css/
│   ├── images/
│   ├── js/
│   ├── loads/
│   ├── materialize/
├── templates
│   ├── account/
│   │   ├── login.html
│   │   ├── register.html
│   ├── chat.html
├── account.py
├── main.py
├── settings.py
└── requirements.txt
```

## **License**

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

---

### **Contact**

- **Author**: [zdilby](https://github.com/zdilby)
- **Project Link**: [TSAI](https://github.com/zdilby/tsai)

---
