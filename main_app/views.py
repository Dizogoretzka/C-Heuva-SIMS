from django.contrib import messages
from django.contrib.auth import authenticate, login, logout
from django.http import HttpResponse
from django.shortcuts import redirect, render, reverse


# Create your views here.


def login_page(request):
    if request.user.is_authenticated:
        if request.user.user_type == '1':
            return redirect(reverse("admin_home"))
        if request.user.user_type == '2':
            return redirect(reverse("student_home"))
    return render(request, 'main_app/landing.html')


def role_login(request, role):
    if role not in ('admin', 'learner'):
        return redirect(reverse('login_page'))
    if request.user.is_authenticated:
        if request.user.user_type == '1':
            return redirect(reverse('admin_home'))
        if request.user.user_type == '2':
            return redirect(reverse('student_home'))
    return render(request, 'main_app/login.html', {'selected_role': role})


def doLogin(request, **kwargs):
    if request.method != 'POST':
        return HttpResponse("<h4>Denied</h4>")
    role = request.POST.get('role')
    if role not in ('admin', 'learner'):
        return redirect(reverse('login_page'))
    login_url = reverse('role_login', args=[role])
    expected_user_type = '1' if role == 'admin' else '2'
    user = authenticate(
        request,
        username=request.POST.get('email'),
        password=request.POST.get('password'),
    )
    if user is None or user.user_type != expected_user_type:
        messages.error(request, "Invalid details")
        return redirect(login_url)
    login(request, user)
    if role == 'admin':
        return redirect(reverse("admin_home"))
    return redirect(reverse("student_home"))



def logout_user(request):
    if request.user != None:
        logout(request)
    return redirect("/")


def showFirebaseJS(request):
    data = """
    // Give the service worker access to Firebase Messaging.
// Note that you can only use Firebase Messaging here, other Firebase libraries
// are not available in the service worker.
importScripts('https://www.gstatic.com/firebasejs/7.22.1/firebase-app.js');
importScripts('https://www.gstatic.com/firebasejs/7.22.1/firebase-messaging.js');

// Initialize the Firebase app in the service worker by passing in
// your app's Firebase config object.
// https://firebase.google.com/docs/web/setup#config-object
firebase.initializeApp({
    apiKey: "AIzaSyBarDWWHTfTMSrtc5Lj3Cdw5dEvjAkFwtM",
    authDomain: "sms-with-django.firebaseapp.com",
    databaseURL: "https://sms-with-django.firebaseio.com",
    projectId: "sms-with-django",
    storageBucket: "sms-with-django.appspot.com",
    messagingSenderId: "945324593139",
    appId: "1:945324593139:web:03fa99a8854bbd38420c86",
    measurementId: "G-2F2RXTL9GT"
});

// Retrieve an instance of Firebase Messaging so that it can handle background
// messages.
const messaging = firebase.messaging();
messaging.setBackgroundMessageHandler(function (payload) {
    const notification = JSON.parse(payload);
    const notificationOption = {
        body: notification.body,
        icon: notification.icon
    }
    return self.registration.showNotification(payload.notification.title, notificationOption);
});
    """
    return HttpResponse(data, content_type='application/javascript')
