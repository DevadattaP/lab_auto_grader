#include <stdio.h>

int main() {
    char ch1, ch2;
    int diff;

    scanf(" %c", &ch1);
    scanf(" %c", &ch2);

    diff = ch1 - ch2;
    if (diff < 0)
        diff = -diff;

    printf("Character 1 : %c (%d)\n", ch1, ch1);
    printf("Character 2 : %c (%d)\n\n", ch2, ch2);

    if (diff == 10) {
        printf("SECRET PAIR FOUND\n");
        printf("Secret Code : %c%c%d\n", ch1, ch2, ch1);
    } else {
        printf("NO SECRET PAIR\n");
    }

    return 0;
}
