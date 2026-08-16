#include <stdio.h>
int main() {
    long long n;
    int steps = 0;
    scanf("%lld", &n);

    if (n <= 0) {
        printf("INVALID INPUT\n");
        return 0;
    }

    printf("%lld", n);
    while (n != 1) {
        if (n % 2 == 0)
            n = n / 2;
        else
            n = 3 * n + 1;
        printf(" %lld", n);
        steps++;
    }
    printf("\n");
    printf("Steps = %d\n", steps);

    return 0;
}
