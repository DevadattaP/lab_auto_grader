#include <stdio.h>
int main() {
    long long n, temp;
    scanf("%lld", &n);

    if (n <= 0) {
        printf("INVALID INPUT\n");
        return 0;
    }
    if (n == 1) {
        printf("1 has no prime factors\n");
        return 0;
    }

    temp = n;
    for (long long p = 2; p * p <= temp; p++) {
        if (temp % p == 0) {
            int count = 0;
            while (temp % p == 0) {
                temp /= p;
                count++;
            }
            printf("%lld^%d\n", p, count);
        }
    }
    if (temp > 1) {
        printf("%lld^1\n", temp);
    }

    return 0;
}
