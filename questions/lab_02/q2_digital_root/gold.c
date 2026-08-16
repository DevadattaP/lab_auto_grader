#include <stdio.h>
int main() {
    long long n, temp, sum;
    int rounds = 0;

    scanf("%lld", &n);

    if (n < 0) {
        printf("INVALID INPUT\n");
        return 0;
    }

    if (n >= 10) {
        do {
            sum = 0;
            temp = n;
            while (temp != 0) {
                sum += temp % 10;
                temp /= 10;
            }
            n = sum;
            rounds++;
        } while (n >= 10);
    }

    printf("Digital Root = %lld\n", n);
    printf("Rounds = %d\n", rounds);

    return 0;
}
