#include <stdio.h>

int main() {
    int n;
    scanf("%d", &n);

    if (n <= 0) {
        printf("INVALID INPUT\n");
        return 0;
    }

    long long a = 0, b = 1, next;
    for (int i = 0; i < n; i++) {
        if (i == 0)
            printf("%lld", a);
        else
            printf(" %lld", a);
        next = a + b;
        a = b;
        b = next;
    }
    printf("\n");

    return 0;
}
