class WatchWidget {
    //static name = 'watch';
    //static display_name = 'Watch Window';
    static initialize(container, sate) {
        container.getElement().html('<h2 style="text-align:center">Watch!</h2>');
    }

    static name() {
        return 'watch';
    }
    static display_name() {
        return 'Watch Window';
    }

    static icon_path() {
        return 'assets/img/eye-96x128.png';
    }
}